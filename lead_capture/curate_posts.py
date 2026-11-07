"""
curate_posts.py - go through the Reddit posts you already saved and write a reply that recommends properties from YOUR listings.

For every pending post it:
  1. finds the listings in your Supabase table that fit the post (same search as the rest of the project; sold, booked and
     on-hold listings are never used, and if the post names an area only listings in that area are used),
  2. asks Claude to label the post (buyer, advice, discussion, ad, other) and, only when a listing really fits, to write a short
     reply that recommends one or two of them, using the price and size text exactly as stored,
  3. checks the reply (no link, phone number or email; it must name a real listing; every number must come from the listing or
     the post) and signs it with your business name.
Posts with no fitting listing, and discussion or ad posts, are hidden from the dashboard queue and counted.

It does NOT call Apify, post anything to Reddit, or message anyone. A person reads, edits and posts each reply.

Run it on your PC, in lead_capture (where .env has ANTHROPIC_API_KEY and the Supabase keys, and tfidf_vectorizer.pkl is):
  python curate_posts.py --dry-run --show --limit 10   # preview, saves nothing
  python curate_posts.py                               # curate every pending post that has not been done
  python curate_posts.py --force                       # redo everything, including the search for listings
  python curate_posts.py --only 77                     # one post by id
"""
import argparse
import json
import os
import re

import apify_source as A
import leads as L

TYPES = ("buyer", "advice", "discussion", "ad", "other")

SYSTEM = """You help a Hyderabad property dealer answer public Reddit posts from people looking for property.
You are given one post and a numbered list of the dealer's available listings. Decide the post's type and, only if a listing genuinely fits, write the reply.

type:
- buyer: wants to buy, rent or invest and is asking for options, areas or advice.
- advice: asks a question about buying where one of our listings could be a useful answer.
- discussion: opinion, news, a review, a market or policy chat, or a question that is not about choosing a property.
- ad: a broker, agent or builder promotion.
- other: anything else.

worth_replying is true ONLY when type is buyer or advice AND at least one listing clearly fits what they asked (property type, area, and budget if they gave one). Otherwise false, and reply is "".

When worth_replying is true, write "reply":
- 45 to 100 words, friendly and plain, no markdown, no emojis, no hype.
- Start by showing you understood what they want, in one short clause.
- Recommend one or two of the listings that fit. For each, give the project name, type or BHK, location, and the price and size exactly as written in the listing text. Copy numbers and units exactly. Never convert, add up, compare or guess.
- Availability is only what the listing's status text says. Do not promise a unit is free.
- Do not invent amenities, returns, appreciation, possession dates, discounts or urgency.
- No link, email or phone number. End by inviting them to send a message if they want details.
- Do not sign it. The signature is added separately.

Reply with ONLY a JSON object: {"type": "...", "worth_replying": true or false, "reason": "one short sentence", "reply": "..."}."""


SYSTEM_ASK = """You help a Hyderabad property dealer answer a public Reddit post from someone looking for property.
The dealer has no listing that fits yet. Write a short reply (35 to 80 words, plain, friendly, no markdown, no emojis):
- Respond to what THIS person actually asked, in one sentence that shows you read it. Do not give legal, RERA, price or market facts and do not claim anything about any project.
- Ask for what you need to help: budget, preferred area and whether it is to live in or to invest (only ask what the post does not already say).
- Say you are a property dealer in Hyderabad and can share a few options once you know that.
- No link, email, phone number or invented numbers. Do not sign it.
Reply with ONLY a JSON object: {"reply": "..."}."""

ASK_FALLBACK = ("Thanks for sharing your requirement. To point you to the right options, could you tell me your budget, "
                "preferred area and whether it is to live in or to invest? I'm a property dealer in Hyderabad and can share "
                "a few options that fit once I know that.")


def safe_ask(reply: str, post_text: str) -> bool:
    r = (reply or "").strip()
    return 20 <= len(r.split()) <= 100 and not _BAD.search(r) and _numbers(r) <= _numbers(post_text)


def ask_reply(post: dict, llm) -> str:
    text = f"{post.get('title') or ''} {post.get('body') or ''}"
    try:
        obj = parse_json(llm(SYSTEM_ASK, f"Post:\n{post.get('title') or ''}\n{(post.get('body') or '')[:900]}\n\nReturn the JSON object."))
        reply = str((obj or {}).get("reply") or "").strip()
    except Exception:
        reply = ""
    return sign(reply if safe_ask(reply, text) else ASK_FALLBACK)


def _call_claude(system: str, user: str) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    r = client.messages.create(model=L.CLAUDE_MODEL, max_tokens=700, temperature=0.3, system=system,
                               messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if b.type == "text")


def parse_json(text: str) -> dict | None:
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


_BAD = re.compile(r"(https?://|www\.|\S+@\S+\.\S+|\b\d{10}\b)", re.I)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(text or "")}


def facts_text(options: list[dict]) -> str:
    return " ".join(str(v) for m in options for v in m.values() if v)


def safe_reply(reply: str, options: list[dict], post_text: str) -> bool:
    """The reply must be a sensible length, carry no link, email or phone number, name one of the listings we gave,
    and use only numbers that appear in those listings or in the post itself."""
    r = (reply or "").strip()
    if not 25 <= len(r.split()) <= 140 or _BAD.search(r):
        return False
    low = r.lower()
    if not any(str(m.get("project_name") or "").strip().lower() in low for m in options if m.get("project_name")):
        return False
    return _numbers(r) <= (_numbers(facts_text(options)) | _numbers(post_text))


def sign(reply: str) -> str:
    return f"{reply.strip()}\n\n- {L.BRAND_NAME}"


def curate_one(post: dict, options: list[dict], llm=None, ask_when_no_listing: bool = False) -> dict:
    """Returns {type, worth_replying, reason, reply}. Never raises on bad model output.
    worth_replying is None when the answer could not be read (the post is left as it was)."""
    llm = llm or _call_claude
    listings = "\n".join(f"{i}. {A._listing_line(m)}  [status: {m.get('status') or 'not stated'}]" for i, m in enumerate(options, 1)) or "(none)"
    user = (f"Post:\n{post.get('title') or ''}\n{(post.get('body') or '')[:900]}\n\n"
            f"Available listings:\n{listings}\n\nReturn the JSON object.")
    try:
        obj = parse_json(llm(SYSTEM, user))
    except Exception as e:
        return {"type": "other", "worth_replying": None, "reason": f"model call failed: {e}"[:160], "reply": None}
    if not obj or obj.get("type") not in TYPES:
        return {"type": "other", "worth_replying": None, "reason": "the model's answer could not be read", "reply": None}
    kind = obj["type"]
    reason = str(obj.get("reason") or "")[:200]
    if kind not in ("buyer", "advice"):
        return {"type": kind, "worth_replying": False, "reason": reason or f"a {kind} post", "reply": None}
    if not options or not bool(obj.get("worth_replying")):
        if ask_when_no_listing:
            return {"type": kind, "worth_replying": True, "reply_mode": "ask",
                    "reason": "no listing fits yet, so the reply asks for budget and area", "reply": ask_reply(post, llm)}
        return {"type": kind, "worth_replying": False, "reason": ("no listing in our data fits this post" if not options else reason or "no listing fits what they asked"), "reply": None}
    text = f"{post.get('title') or ''} {post.get('body') or ''}"
    reply = str(obj.get("reply") or "").strip()
    if safe_reply(reply, options, text):
        return {"type": kind, "worth_replying": True, "reply_mode": "listings", "reason": reason, "reply": sign(reply)}
    # The model's text did not pass the check: build a plain reply straight from the listing text instead.
    return {"type": kind, "worth_replying": True, "reply_mode": "listings", "reason": (reason + " (plain reply used: the written one failed the check)").strip(),
            "reply": sign(A._template_public_reply(options))}


def split_post(text: str) -> dict:
    lines = (text or "").split("\n", 1)
    return {"title": lines[0], "body": lines[1] if len(lines) > 1 else ""}


def curate(store, llm=None, matcher=None, *, dry_run=False, force=False, limit=500, only=None, show=False, ask=False) -> dict:
    matcher = matcher or L.match_listings
    stats = {"checked": 0, "curated": 0, "with_reply": 0, "set_aside": 0, "skipped": 0, "unreadable": 0}
    for row in store.list_by_source(A.SOURCE_NAME, limit=limit):
        stats["checked"] += 1
        payload = dict(row.get("raw_payload") or {})
        if only is not None and row.get("id") != only:
            stats["skipped"] += 1
            continue
        if (row.get("reply_status") or "pending") != "pending" or row.get("status") != "needs_review":
            stats["skipped"] += 1
            continue
        if payload.get("curation") and not force and only is None:
            stats["skipped"] += 1
            continue
        post = split_post(row.get("interest_text"))
        if payload.get("matches") and not force:
            options = payload["matches"] or []
        else:
            try:
                found = matcher({"interest_text": row.get("interest_text"), "category": row.get("category")})
            except Exception as e:
                print(f"  post {row.get('id')}: listing search failed ({e})")
                stats["skipped"] += 1
                continue
            options = A.relevant(post, A.offerable(found))[:3]
            payload["matches"] = [{k: m.get(k) for k in ("project_name", "type_or_bhk", "location", "price_raw", "size_raw", "status")}
                                  for m in options]
            options = payload["matches"]
        payload["subreddit"] = A.subreddit_of(payload.get("url"))
        result = curate_one(post, options, llm, ask_when_no_listing=ask)
        if result["worth_replying"] is None:
            stats["unreadable"] += 1
            if show:
                print(f"  post {row.get('id')}: {result['reason']}")
            continue
        stats["curated"] += 1
        stats["with_reply" if result["worth_replying"] else "set_aside"] += 1
        if show or dry_run:
            names = ", ".join(m["project_name"] for m in options) or "(none)"
            print(f"  post {row.get('id')}: {result['type']:<10} reply={result['worth_replying']!s:<5} listings: {names}")
            print(f"      post: {post['title'][:100]}")
            print(f"      why: {result['reason']}")
            if result["reply"]:
                print(f"      reply: {result['reply'][:300]}")
        if not dry_run:
            payload["curation"] = {"type": result["type"], "worth_replying": result["worth_replying"], "reason": result["reason"],
                                    "reply_mode": result.get("reply_mode")}
            store.update(row["id"], {"raw_payload": payload, "matched_projects": [m["project_name"] for m in options],
                                     "draft_reply": result["reply"]})
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Write replies that recommend your own listings for the Reddit posts you saved.")
    parser.add_argument("--dry-run", action="store_true", help="Show the result; save nothing.")
    parser.add_argument("--show", action="store_true", help="Print each decision and reply.")
    parser.add_argument("--force", action="store_true", help="Redo posts that were already curated, including the search for listings.")
    parser.add_argument("--ask", action="store_true", help="Also reply to buyer/advice posts with no matching listing, by asking for budget and area.")
    parser.add_argument("--only", type=int, help="Curate one post by its id.")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY in .env first.")
    print(curate(L.SupabaseLeadStore(), dry_run=args.dry_run, force=args.force, limit=args.limit, only=args.only, show=args.show, ask=args.ask))
