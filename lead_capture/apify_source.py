"""
apify_source.py - turn public posts found by Apify into leads for review.

What this does
  1. Runs an Apify Actor (a ready-made scraper, e.g. a Reddit scraper) with your
     keywords - or reads a saved JSON file of results, so you can test for free.
  2. Scores each public post for real BUYING/RENTING intent and drops sellers,
     agents' ads and news.
  3. Stores the good ones in your Supabase `leads` table with status
     'needs_review' and consent = false, then alerts the agent on Telegram with
     the post link and a suggested PUBLIC reply.

What it deliberately does NOT do
  - It never looks for phone numbers/emails, never DMs anyone, never sends
    marketing. A public post is a signal, not consent. The agent replies
    publicly with something useful; if the person answers and shares a number,
    that is when a normal consented lead begins.

Setup
  pip install apify-client
  .env:  APIFY_TOKEN=apify_api_...          (Apify Console -> Settings -> Integrations)
         APIFY_ACTOR_ID=<owner~actor-name>  (e.g. the Reddit scraper you picked)

Usage
  python apify_source.py --from-file sample_posts.json --dry-run     # free, offline
  python apify_source.py --limit 20 --dry-run                        # real run, nothing saved/sent
  python apify_source.py --limit 50                                  # real run, saves + alerts

The Actor's INPUT and OUTPUT field names differ between Actors. Put the input
you want in apify_input.json (copy it from the Actor's "Input" tab in Apify
Console). The reader below tries the common output field names; if your Actor
uses others, add them to the *_FIELDS tuples.
"""
import argparse
import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

import leads as L

load_dotenv()
logger = logging.getLogger(__name__)

APIFY_TOKEN = os.getenv("APIFY_TOKEN")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID")
INPUT_FILE = Path(__file__).parent / "apify_input.json"
SOURCE_NAME = os.getenv("APIFY_SOURCE_NAME", "apify_reddit")
MIN_SCORE = int(os.getenv("APIFY_MIN_SCORE", "4"))

# Areas you serve - a mention adds to the score. Edit freely.
AREAS = [a.strip().lower() for a in os.getenv(
    "LEAD_AREAS",
    "hyderabad,secunderabad,gachibowli,kondapur,madhapur,hitech city,kukatpally,"
    "miyapur,shamshabad,abids,banjara hills,jubilee hills,uppal,lb nagar,"
    "narsingi,kokapet,financial district,bachupally,medchal,tellapur"
).split(",") if a.strip()]

# Output field names used by different Actors (first one present wins).
TITLE_FIELDS = ("title", "postTitle", "name")
BODY_FIELDS = ("body", "text", "selftext", "content", "description", "postBody")
URL_FIELDS = ("url", "postUrl", "permalink", "link")
ID_FIELDS = ("id", "parsedId", "postId", "post_id", "name")
AUTHOR_FIELDS = ("username", "author", "user", "authorName")

INTENT_PHRASES = [
    "looking for", "looking to buy", "looking to rent", "want to buy", "want to rent",
    "planning to buy", "planning to invest", "thinking of buying", "need a", "need to buy",
    "suggest", "recommend", "any good", "anyone know", "where to buy", "which area",
    "best area", "help me find", "budget", "worth buying", "should i buy", "can i afford",
]
PROPERTY_WORDS = [
    "flat", "apartment", "bhk", "plot", "villa", "house", "land", "property", "office space",
    "commercial", "shop", "rent", "lease", "resale", "gated community", "builder", "rera",
]
SELLER_OR_SPAM = [
    "for sale by", "we offer", "contact us", "call us", "book now", "limited units",
    "starting from", "pre-launch", "prelaunch", "site visit", "whatsapp me", "visit our",
    "channel partner", "our project", "exclusive offer", "hurry",
]


# ------------------------------------------------------------- reading items --

def _first(item: dict, fields) -> str | None:
    for f in fields:
        v = item.get(f)
        if v not in (None, "", []):
            return str(v)
    return None


def read_item(item: dict) -> dict:
    """Pulls the few fields we need out of one Actor result row."""
    title = _first(item, TITLE_FIELDS) or ""
    body = _first(item, BODY_FIELDS) or ""
    url = _first(item, URL_FIELDS)
    if url and url.startswith("/"):
        url = "https://www.reddit.com" + url
    return {
        "id": _first(item, ID_FIELDS) or url,
        "title": title.strip(),
        "body": body.strip(),
        "url": url,
        "author": _first(item, AUTHOR_FIELDS),
    }


# ------------------------------------------------------------------- scoring --

def intent_score(text: str, areas=None) -> int:
    """Rough 0-8 score for 'this person wants to buy/rent'. Not machine learning -
    just transparent rules you can tune. Printed with each lead so you can see why."""
    t = text.lower()
    areas = AREAS if areas is None else areas
    score = 0
    if any(p in t for p in INTENT_PHRASES):
        score += 2
    if L.detect_category(t) or any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in PROPERTY_WORDS) \
            or re.search(r"\b\d\s?bhk\b", t):
        score += 2
    if any(a in t for a in areas):
        score += 2
    if "?" in t:
        score += 1
    score -= 3 * sum(1 for s in SELLER_OR_SPAM if s in t)
    return score


# -------------------------------------------------------------- public reply --

_NOT_OFFERABLE = ("sold", "booked", "hold", "not available", "unavailable", "withdrawn")


def offerable(matches: list[dict]) -> list[dict]:
    """Only listings we can honestly offer: drops sold, booked and on-hold ones."""
    out = []
    for m in matches or []:
        status = str(m.get("status") or "").lower()
        if any(word in status for word in _NOT_OFFERABLE):
            continue
        if m.get("project_name"):
            out.append(m)
    return out


def subreddit_of(url) -> str | None:
    m = re.search(r"/r/([A-Za-z0-9_]+)/", url or "")
    return m.group(1).lower() if m else None


def _listing_line(m: dict) -> str:
    parts = [m.get("project_name"), m.get("type_or_bhk"), m.get("location")]
    line = ", ".join(p for p in parts if p)
    extras = [f"{label} {m[key]}" for label, key in (("price", "price_raw"), ("size", "size_raw")) if m.get(key)]
    return line + (f" ({'; '.join(extras)})" if extras else "")


def _llm_public_reply(post: dict, options: list[dict]) -> str:
    from anthropic import Anthropic

    listings = "\n".join(f"{i}. {_listing_line(m)}" for i, m in enumerate(options, 1))
    prompt = f"""Write a PUBLIC Reddit reply from a Hyderabad property dealer to someone looking to buy.

Their post:
{(post.get('title') or '')}
{(post.get('body') or '')[:700]}

Listings we can suggest (the ONLY facts you may use):
{listings}

Rules:
- 60 to 90 words, friendly and plain, no emojis, no markdown, no hype.
- Answer their actual question first in one sentence, without claims you cannot support from the post.
- Then mention at most two of the listings above that fit what they asked. Quote price and size text exactly as written. Never convert units, add totals or guess.
- Do not invent amenities, returns, appreciation, possession dates, discounts or urgency. Do not say anything is guaranteed.
- Do not include a phone number, email or link. Invite them to send a message if they want details.
- Output the reply text only. Do not sign it."""
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=L.CLAUDE_MODEL, max_tokens=300, temperature=0.3,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _template_public_reply(options: list[dict]) -> str:
    lines = "; ".join(_listing_line(m) for m in options[:2])
    return ("Happy to help. Based on what you describe, these may be worth a look: " + lines +
            ". If you'd like details, send me a message with your budget and preferred area.")


def suggest_public_reply(post: dict, matches: list[dict]) -> str | None:
    """A PUBLIC reply drafted ONLY from our own available listings, for the agent to
    edit and post by hand. Returns None when no listing can honestly be offered, so the
    dashboard falls back to plain advice. It never contains a phone number."""
    options = offerable(matches)[:3]
    if not options:
        return None
    body = None
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            body = _llm_public_reply(post, options)
        except Exception:
            logger.exception("LLM public reply failed; using template")
    body = body or _template_public_reply(options)
    return f"{body}\n\n- {L.BRAND_NAME}"


# ------------------------------------------------------------------- Apify ---

def run_actor(actor_id: str, run_input: dict, limit: int):
    """Runs the Actor and returns up to `limit` result rows. Uses Apify credit."""
    try:
        from apify_client import ApifyClient
    except ImportError:
        raise SystemExit("Install the client first:  pip install apify-client")
    if not APIFY_TOKEN:
        raise SystemExit("Set APIFY_TOKEN in .env (Apify Console -> Settings -> Integrations).")
    client = ApifyClient(APIFY_TOKEN)
    run = client.actor(actor_id).call(run_input=run_input)
    if run is None:
        raise SystemExit("The Apify run did not finish. Check the run in Apify Console.")
    # Newer apify-client versions return a Run object; older ones return a dict.
    if isinstance(run, dict):
        dataset_id = run.get("defaultDatasetId") or run.get("default_dataset_id")
    else:
        dataset_id = getattr(run, "default_dataset_id", None) or getattr(run, "defaultDatasetId", None)
    if not dataset_id:
        raise SystemExit("The Apify run returned no dataset id.")
    return list(client.dataset(dataset_id).iterate_items(limit=limit))


def load_input() -> dict:
    if not INPUT_FILE.exists():
        raise SystemExit(
            f"Create {INPUT_FILE.name} with the Actor's input JSON "
            "(copy it from the Actor's Input tab in Apify Console, then add your keywords).")
    return json.loads(INPUT_FILE.read_text(encoding="utf-8"))


# ----------------------------------------------------------------- pipeline --

def to_lead(post: dict, score: int) -> dict:
    text = f"{post['title']}\n{post['body']}".strip()
    return L.build_lead(
        SOURCE_NAME,
        source_ref=post["id"],
        name=post["author"],                       # public username only
        interest_text=text[:600],
        consent=False,                             # a public post is not consent
        raw_payload={"url": post["url"], "intent_score": score},
    )


def process_items(items: list[dict], store, *, min_score=MIN_SCORE, dry_run=False, matcher=None) -> dict:
    stats = {"seen": 0, "skipped_low_intent": 0, "duplicate": 0, "saved": 0}
    for raw in items:
        stats["seen"] += 1
        post = read_item(raw)
        if not post["id"] or len(f"{post['title']}{post['body']}") < 20:
            stats["skipped_low_intent"] += 1
            continue
        score = intent_score(f"{post['title']} {post['body']}")
        if score < min_score:
            stats["skipped_low_intent"] += 1
            continue

        lead = to_lead(post, score)
        try:
            matches = (matcher or L.match_listings)(lead)
        except Exception:
            logger.exception("listing match failed; continuing without matches")
            matches = []
        options = offerable(matches)[:3]
        lead["raw_payload"]["subreddit"] = subreddit_of(post["url"])
        lead["raw_payload"]["matches"] = [
            {k: m.get(k) for k in ("project_name", "type_or_bhk", "location", "price_raw", "size_raw", "status")}
            for m in options]
        reply = suggest_public_reply(post, options)
        extra = (f"\nPOST: {post['url']}\nIntent score: {score}\n\n"
                 f"Suggested PUBLIC reply (edit before posting):\n{reply or '(no listing matched - advice only)'}\n"
                 "Do not DM unless they reply or ask you to.")

        def notifier(text, dry_run=False, _extra=extra):
            # process_lead's own alert already says 'needs_review - no phone/email'.
            return L.notify_agent(text + _extra, dry_run=dry_run)

        result = L.process_lead(lead, store, matcher=lambda l, m=matches: m, notifier=notifier, dry_run=dry_run)
        if result["status"] != "duplicate":
            try:
                store.update(result["lead_id"], {"matched_projects": [m["project_name"] for m in options],
                                                 "draft_reply": reply})
            except Exception:
                logger.exception("could not save the listing-based draft; the post is still saved")
        stats["duplicate" if result["status"] == "duplicate" else "saved"] += 1
    return stats


def get_store(dry_run: bool):
    if dry_run or not os.getenv("SUPABASE_URL"):
        return L.InMemoryLeadStore()
    return L.SupabaseLeadStore()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find buying/renting intent posts via Apify and store them as leads.")
    parser.add_argument("--from-file", help="Read saved Actor results (JSON list) instead of running the Actor - free.")
    parser.add_argument("--limit", type=int, default=30, help="Max posts to read from the Actor's dataset.")
    parser.add_argument("--min-score", type=int, default=MIN_SCORE)
    parser.add_argument("--dry-run", action="store_true",
                        help="Nothing is saved to Supabase and no Telegram alert is sent.")
    args = parser.parse_args()

    if args.from_file:
        items = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    else:
        if not APIFY_ACTOR_ID:
            raise SystemExit("Set APIFY_ACTOR_ID in .env, e.g. APIFY_ACTOR_ID=username~actor-name")
        items = run_actor(APIFY_ACTOR_ID, load_input(), args.limit)
        Path("apify_last_results.json").write_text(json.dumps(items, indent=2, default=str), encoding="utf-8")
        print(f"Got {len(items)} rows (saved to apify_last_results.json - re-run free with --from-file).")
        if items:
            print("Fields in the first row:", sorted(items[0].keys()))

    print(process_items(items, get_store(args.dry_run), min_score=args.min_score, dry_run=args.dry_run))
