"""
supabase_retrieval.py

Three-tier retrieval against Supabase (same architecture as the local
ChromaDB version), PLUS a semantic cache layer in front of the LLM call —
the same pattern from the concept_code/semantic_cache.py we built earlier,
now persisted in Supabase's query_cache table instead of an in-memory list.

Flow for every query:
  1. Embed the query with our TF-IDF vectorizer (must be the SAME fitted
     vectorizer used in supabase_store.py — loaded from the pickle file).
  2. CHECK CACHE FIRST — if a semantically similar query was answered
     before (above threshold), return that cached answer, skip retrieval
     AND the LLM call entirely. This is the actual cost/latency win.
  3. On a cache miss: run the three-tier retrieval (hard filter -> semantic
     search via Supabase's match_properties function -> fallback if empty).
  4. Generate the LLM answer, then WRITE it to the cache for next time.
"""
import os
import pickle
from dataclasses import dataclass
from supabase import create_client, Client
from dotenv import load_dotenv


load_dotenv()

VECTORIZER_PATH = "./tfidf_vectorizer.pkl"
CACHE_SIMILARITY_THRESHOLD = 0.92  # tune this — see note in answer_query()


@dataclass
class RetrievalResult:
    project_name: str
    category: str
    location: str
    type_or_bhk: str
    price_raw: str
    size_raw: str
    status: str
    contact_info: str
    similarity: float
    match_tier: str
    landmark:str
    


def get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY environment variables first.")
    return create_client(url, key)


def load_vectorizer():
    with open(VECTORIZER_PATH, "rb") as f:
        return pickle.load(f)


def embed_query(query: str, vectorizer) -> list[float]:
    return vectorizer.transform([query]).toarray()[0].tolist()


def check_cache(supabase: Client, query_embedding: list[float]) -> dict | None:
    """
    Checks the semantic cache. Returns the cached response dict if a
    similar-enough query was answered before, else None.

    IMPORTANT (same lesson as your own research paper / the silent-failure
    concept): a badly-calibrated threshold here is dangerous. Too low, and
    genuinely different questions get served a wrong cached answer,
    confidently, with nothing looking broken — the exact fill-rate/accuracy
    decoupling pattern from your ablation study, now in a caching context.
    Start conservative (0.92+) and only lower it after real testing shows
    it's safe for your query patterns.
    """
    result = supabase.rpc("match_cached_query", {
        "query_embedding": query_embedding,
        "similarity_threshold": CACHE_SIMILARITY_THRESHOLD,
        "match_count": 1,
    }).execute()

    if result.data:
        cached = result.data[0]
        # Atomic increment via the RPC function — a plain .update() with a
        # string like "hit_count + 1" would literally overwrite the column
        # with that text, not perform arithmetic. This calls the Postgres
        # function defined in schema.sql instead, which does it correctly.
        supabase.rpc("increment_cache_hit", {"cache_id": cached["id"]}).execute()
        return cached
    return None


def write_cache(supabase: Client, query: str, query_embedding: list[float], response: str):
    supabase.table("query_cache").insert({
        "query_text": query,
        "query_embedding": query_embedding,
        "response_text": response,
    }).execute()


def retrieve(supabase: Client, query_embedding: list[float],
             category: str | None = None, location: str | None = None,
             n_results: int = 5) -> list[RetrievalResult]:
    """Tier 1 + 2: hard filter + semantic search, via the match_properties
    Postgres function (does the vector math in the database)."""
    result = supabase.rpc("match_properties", {
        "query_embedding": query_embedding,
        "match_category": category,
        "match_location": location,
        "match_count": n_results,
    }).execute()

    match_tier = "filtered" if (category or location) else "unfiltered"

    # Tier 3: filter matched nothing -> fall back to unfiltered
    if not result.data and (category or location):
        result = supabase.rpc("match_properties", {
            "query_embedding": query_embedding,
            "match_category": None,
            "match_count": n_results,
        }).execute()
        match_tier = "fallback_unfiltered"

    return [
        RetrievalResult(
            project_name=row["project_name"], category=row["category"],
            location=row["location"], type_or_bhk=row["type_or_bhk"],
            price_raw=row["price_raw"], size_raw=row["size_raw"],
            status=row["status"], contact_info=row["contact_info"],
            landmark=row["landmark"] or "",
            similarity=row["similarity"], match_tier=match_tier,
        )
        for row in result.data
    ]


if __name__ == "__main__":
    print("This module is meant to be imported by bot.py — see bot.py for")
    print("the full pipeline including the LLM call. Run bot.py to test end-to-end.")
