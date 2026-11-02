"""
reddit_review.py - the human-in-the-loop queue for Reddit (public-post) leads.

Flow this supports (nothing is ever sent automatically):
  1. apify_source.py finds buying/renting posts and stores them as leads
     (status needs_review, consent = false).
  2. THIS tool shows the agent the next posts to answer, each with a helpful
     draft PUBLIC reply. The agent edits it and posts it by hand on Reddit.
  3. The agent marks what happened:
        replied        - public reply posted
        they_replied   - the person answered or asked for a message
        shared_contact - the person themselves gave a phone number AND agreed
                         to be contacted -> becomes a normal consented lead
        skipped        - not worth a reply / sub forbids it
  4. Only a shared_contact lead is ever eligible for calls or marketing.

Guard rails
  - REDDIT_REPLIES_PER_DAY (default 5): the queue stops handing out posts after that
    many are marked replied today, so the account never looks like spam.
  - Never the same author twice within AUTHOR_COOLDOWN_DAYS (default 14).
  - Subreddits listed in REDDIT_BRAND_OK_SUBS (comma list) may get a reply that mentions
    the business; everywhere else the draft is advice-only with no brand or links.
  - No DMs, no scraping of contact details; phone/email only via `contact`
    and only with an explicit consent note.

Usage
  python reddit_review.py next [--n 5]          # posts to answer now, with drafts
  python reddit_review.py list [--status replied]
  python reddit_review.py mark <id> replied|they_replied|skipped [--note "..."]
  python reddit_review.py contact <id> --phone 98xxxxxxxx --consent "They wrote 'call me on ...' in a comment on <date>"
  python reddit_review.py --demo
"""
import argparse
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

import leads as L

load_dotenv()

SOURCE_NAME = os.getenv("APIFY_SOURCE_NAME", "apify_reddit")
REPLIES_PER_DAY = int(os.getenv("REDDIT_REPLIES_PER_DAY", "5"))
AUTHOR_COOLDOWN_DAYS = int(os.getenv("AUTHOR_COOLDOWN_DAYS", "14"))
BRAND_OK_SUBS = {s.strip().lower() for s in os.getenv("REDDIT_BRAND_OK_SUBS", "").split(",") if s.strip()}
IST = timezone(timedelta(hours=5, minutes=30))
VALID_MARKS = ("replied", "they_replied", "skipped")


# ------------------------------------------------------------------ helpers --

def subreddit_of(url: str | None) -> str | None:
    m = re.search(r"/r/([A-Za-z0-9_]+)/", url or "")
    return m.group(1).lower() if m else None


def _parse(ts):
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def replied_today(rows: list[dict], now: datetime) -> int:
    day = now.astimezone(IST).date()
    return sum(1 for r in rows
               if r.get("reply_status") == "replied" and _parse(r.get("replied_at"))
               and _parse(r["replied_at"]).astimezone(IST).date() == day)


def recently_answered_authors(rows: list[dict], now: datetime) -> set[str]:
    cutoff = now - timedelta(days=AUTHOR_COOLDOWN_DAYS)
    return {(r.get("name") or "").lower() for r in rows
            if r.get("reply_status") in ("replied", "they_replied", "shared_contact")
            and r.get("name") and _parse(r.get("replied_at")) and _parse(r["replied_at"]) >= cutoff}


# -------------------------------------------------------------------- draft --

AREA_TIPS = {
    "plot": "For plots, check the layout is HMDA/DTCP approved, ask for the LP/RERA number, and verify the title "
            "chain with a lawyer before paying any token.",
    "flat": "For flats, check the RERA registration and approvals, the builder's past deliveries, and compare the "
            "carpet area (not super built-up) when you compare prices.",
    "villa": "For villas, check whether it is a gated community with RERA registration, the plot share in the sale "
             "deed, and the maintenance charges.",
    "commercial": "For commercial space, check the occupancy certificate, parking ratio and current rental yield.",
}


def _tip_for(text: str) -> str:
    t = (text or "").lower()
    for key in ("plot", "villa", "commercial"):
        if key in t:
            return AREA_TIPS[key]
    return AREA_TIPS["flat"]


def draft_public_reply(lead: dict, *, brand_ok: bool = False) -> str:
    """Advice first. The agent must edit this before posting - it is a starting point."""
    text = lead.get("interest_text") or ""
    reply = (_tip_for(text) + " Also, share your budget and preferred area and people here can help you narrow it "
             "down.")
    if brand_ok:
        reply += (f" I'm a property dealer in Hyderabad ({L.BRAND_NAME}) - if you'd like, message me and I'll share "
                  "a few options in your budget, no obligation.")
    return reply


# -------------------------------------------------------------------- queue --

def next_posts(store, *, n: int = 5, now: datetime | None = None) -> dict:
    """The posts to answer now, honouring the daily cap and the author cooldown."""
    now = now or L.utcnow()
    rows = store.list_by_source(SOURCE_NAME)
    left = max(0, REPLIES_PER_DAY - replied_today(rows, now))
    if left == 0:
        return {"posts": [], "reason": f"daily limit of {REPLIES_PER_DAY} replies reached", "left_today": 0}
    blocked = recently_answered_authors(rows, now)
    out = []
    for r in rows:
        if r.get("reply_status", "pending") != "pending" or r.get("status") != "needs_review":
            continue
        if (r.get("name") or "").lower() in blocked:
            continue
        url = (r.get("raw_payload") or {}).get("url")
        sub = subreddit_of(url)
        brand_ok = sub in BRAND_OK_SUBS
        out.append({"id": r["id"], "url": url, "subreddit": sub, "author": r.get("name"),
                    "post": (r.get("interest_text") or "")[:300], "brand_ok": brand_ok,
                    "score": (r.get("raw_payload") or {}).get("intent_score"),
                    "draft": draft_public_reply(r, brand_ok=brand_ok)})
        if len(out) >= min(n, left):
            break
    return {"posts": out, "reason": None, "left_today": left}


def mark(store, lead_id, action: str, *, note: str | None = None, now: datetime | None = None) -> dict:
    if action not in VALID_MARKS:
        raise ValueError(f"action must be one of {VALID_MARKS}")
    now = now or L.utcnow()
    fields = {"reply_status": action, "reply_note": note}
    if action in ("replied", "they_replied"):
        fields["replied_at"] = now.isoformat()
    return store.update(lead_id, fields)


def record_contact(store, lead_id, *, phone: str, consent_text: str, now: datetime | None = None) -> dict:
    """The person themselves shared a number and agreed to be contacted. Needs a written consent note."""
    if not (consent_text or "").strip() or len(consent_text.strip()) < 15:
        raise ValueError("Write down what they said that shows they agreed to be contacted (at least a sentence).")
    norm = L.normalize_phone(phone)
    if not norm:
        raise ValueError("That is not a valid Indian mobile number.")
    now = now or L.utcnow()
    return store.update(lead_id, {
        "phone": phone, "phone_normalized": norm, "consent": True, "consent_at": now.isoformat(),
        "consent_text": consent_text.strip(), "status": "new", "reply_status": "shared_contact",
        "replied_at": now.isoformat()})


# ---------------------------------------------------------------------- CLI --

def _print_posts(result: dict) -> None:
    if not result["posts"]:
        print(result["reason"] or "Nothing to answer right now.")
        return
    print(f"{len(result['posts'])} post(s) to answer ({result['left_today']} replies left today)\n")
    for p in result["posts"]:
        print(f"#{p['id']}  r/{p['subreddit']}  u/{p['author']}  intent {p['score']}")
        print(f"  {p['url']}")
        print(f"  POST: {p['post']}")
        print(f"  DRAFT (edit, then post by hand): {p['draft']}")
        if not p["brand_ok"]:
            print("  note: advice-only - check this sub's rules before mentioning the business")
        print()


def _store():
    return L.SupabaseLeadStore() if os.getenv("SUPABASE_URL") else L.InMemoryLeadStore()


def _demo() -> None:
    import apify_source as A
    store = L.InMemoryLeadStore()
    A.process_items([{"id": "t3_demo", "title": "Looking for a plot near Shamshabad under 50 lakh, advice?",
                      "body": "Is it safe to buy there?", "url": "/r/hyderabad/comments/demo/x/",
                      "username": "demo_user"}], store, dry_run=True, matcher=lambda lead: [])
    _print_posts(next_posts(store))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Review queue for Reddit (public-post) leads.")
    ap.add_argument("--demo", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("next"); p.add_argument("--n", type=int, default=5)
    p = sub.add_parser("list"); p.add_argument("--status", default=None)
    p = sub.add_parser("mark"); p.add_argument("id", type=int); p.add_argument("action", choices=VALID_MARKS)
    p.add_argument("--note")
    p = sub.add_parser("contact"); p.add_argument("id", type=int)
    p.add_argument("--phone", required=True); p.add_argument("--consent", required=True)
    args = ap.parse_args()

    if args.demo:
        _demo()
    elif args.cmd == "next":
        _print_posts(next_posts(_store(), n=args.n))
    elif args.cmd == "list":
        for r in _store().list_by_source(SOURCE_NAME):
            if args.status is None or r.get("reply_status") == args.status:
                print(r["id"], r.get("reply_status", "pending"), r.get("name"), (r.get("raw_payload") or {}).get("url"))
    elif args.cmd == "mark":
        mark(_store(), args.id, args.action, note=args.note); print("ok")
    elif args.cmd == "contact":
        record_contact(_store(), args.id, phone=args.phone, consent_text=args.consent); print("ok - now a consented lead")
    else:
        ap.print_help()