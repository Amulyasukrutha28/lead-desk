"""
leads.py — capture a lead, match it to listings, draft a first reply, alert the agent.

Flow for every lead (any source):
  1. Normalise the phone number; drop retries/duplicates (same source id, or
     the same phone within DEDUPE_WINDOW_HOURS).
  2. Store it in Supabase's `leads` table (see schema.sql, section 7).
  3. Match it to your real listings using the SAME retrieval as bot.py.
  4. Draft a short first reply (Claude if ANTHROPIC_API_KEY is set, otherwise a
     plain template) grounded ONLY in the matched listings.
  5. Send the agent a Telegram alert with the lead's details, the draft, and a
     tap-to-send WhatsApp link with the draft pre-filled.

Why the agent sends the message instead of the system: sending WhatsApp
messages automatically needs WhatsApp Business API approval and pre-approved
templates. A pre-filled wa.me link gets the same speed (reply in minutes) with
no approval, and a human sees every message before it goes out.

Consent (DPDP Act): a lead is only drafted a reply if `consent` is true — a
Meta lead form submission counts, a lead typed in by hand without consent does
not. Leads without consent are stored and flagged, but no outreach is drafted.

Environment variables (all optional except where noted):
  SUPABASE_URL, SUPABASE_KEY      leads storage (service_role key)
  ANTHROPIC_API_KEY, CLAUDE_MODEL reply drafting (falls back to a template)
  TELEGRAM_BOT_TOKEN              same bot token telegram_bot.py uses
  AGENT_TELEGRAM_CHAT_ID          where alerts go (message the bot, then see
                                  https://api.telegram.org/bot<TOKEN>/getUpdates)
  AGENT_RERA_NO                   the AGENT's RERA registration number — added
                                  to every drafted reply. If unset, replies go
                                  out without it and the alert warns you.
  BRAND_NAME, BRAND_CONTACT_NUMBER
"""
import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DEDUPE_WINDOW_HOURS = 24
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
BRAND_NAME = os.getenv("BRAND_NAME", "Vantage Properties")
BRAND_CONTACT_NUMBER = os.getenv("BRAND_CONTACT_NUMBER", "9966442577")

# Same keyword map as telegram_bot.py / app.py (kept here so importing this
# module doesn't pull in the Telegram polling bot).
CATEGORY_KEYWORDS = {
    "Flats": ["flat", "flats", "apartment", "bhk"],
    "commercials": ["commercial", "office", "shop", "retail", "showroom"],
    "Plots": ["plot", "plots", "land", "acre", "acres"],
    "Villa": ["villa", "villas"],
    "Re-sale": ["resale", "re-sale", "second hand", "used"],
}


# ---------------------------------------------------------------- parsing ---

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_phone(raw) -> str | None:
    """Returns the 10-digit Indian mobile number, or None if it isn't one.
    Handles '+91 99664 42577', '09966442577', '99664-42577', etc."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) < 10:
        return None
    last10 = digits[-10:]
    return last10 if last10[0] in "6789" else None


def detect_category(text: str | None) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", lowered):
                return category
    return None


def parse_meta_field_data(field_data: list[dict]) -> dict:
    """
    Turns Meta's lead `field_data` ([{"name": "full_name", "values": ["A"]}, ...])
    into {name, phone, email, interest_text}. Only Meta's standard field keys
    are treated as contact fields; every other (custom) question is kept as
    "question: answer" text in interest_text, so nothing the person typed is
    lost and a custom question can never be mistaken for a contact field.
    """
    name_parts, phone, email, extras = {}, None, None, []
    for field in field_data or []:
        key = (field.get("name") or "").strip().lower()
        value = ", ".join(str(v) for v in (field.get("values") or [])).strip()
        if not value:
            continue
        if key == "full_name":
            name_parts["full"] = value
        elif key == "first_name":
            name_parts["first"] = value
        elif key == "last_name":
            name_parts["last"] = value
        elif key in ("phone_number", "phone"):
            phone = value
        elif key == "email":
            email = value
        else:
            extras.append(f"{key.replace('_', ' ').strip(' ?')}: {value}")
    name = name_parts.get("full") or " ".join(
        p for p in (name_parts.get("first"), name_parts.get("last")) if p
    ) or None
    return {"name": name, "phone": phone, "email": email, "interest_text": "; ".join(extras) or None}


def build_lead(source: str, *, source_ref: str | None = None, name: str | None = None,
               phone: str | None = None, email: str | None = None,
               interest_text: str | None = None, consent: bool = False,
               consent_text: str | None = None, raw_payload: dict | None = None) -> dict:
    """Builds the normalised lead dict every other function in this file expects."""
    return {
        "source": source,
        "source_ref": source_ref,
        "name": (name or "").strip() or None,
        "phone": phone,
        "phone_normalized": normalize_phone(phone),
        "email": (email or "").strip() or None,
        "interest_text": interest_text,
        "category": detect_category(interest_text),
        "consent": bool(consent),
        "consent_at": utcnow().isoformat() if consent else None,
        "consent_text": consent_text if consent else None,
        "raw_payload": raw_payload,
    }


# ----------------------------------------------------------------- storage --

class InMemoryLeadStore:
    """For tests and --demo runs: same interface as SupabaseLeadStore, no network."""

    def __init__(self):
        self.rows: list[dict] = []

    def find_by_source_ref(self, source, ref):
        return next((r for r in self.rows if r["source"] == source and r["source_ref"] == ref), None)

    def find_recent_by_phone(self, phone_normalized, since_iso):
        hits = [r for r in self.rows
                if r["phone_normalized"] == phone_normalized and r["created_at"] >= since_iso]
        return hits[-1] if hits else None

    def insert(self, row):
        stored = {**row, "id": len(self.rows) + 1, "status": row.get("status", "new"),
                  "created_at": utcnow().isoformat(), "notified_at": None}
        self.rows.append(stored)
        return stored

    def update(self, lead_id, fields):
        row = next(r for r in self.rows if r["id"] == lead_id)
        row.update(fields)
        return row

    def list_by_source(self, source, limit=200):
        rows = [r for r in self.rows if r["source"] == source]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]


class SupabaseLeadStore:
    def __init__(self, client=None):
        if client is None:
            from supabase_retrieval import get_supabase_client
            client = get_supabase_client()
        self.client = client

    def find_by_source_ref(self, source, ref):
        r = (self.client.table("leads").select("*")
             .eq("source", source).eq("source_ref", ref).limit(1).execute())
        return r.data[0] if r.data else None

    def find_recent_by_phone(self, phone_normalized, since_iso):
        r = (self.client.table("leads").select("*")
             .eq("phone_normalized", phone_normalized).gte("created_at", since_iso)
             .order("created_at", desc=True).limit(1).execute())
        return r.data[0] if r.data else None

    def insert(self, row):
        return self.client.table("leads").insert(row).execute().data[0]

    def list_by_source(self, source, limit=200):
        r = (self.client.table("leads").select("*").eq("source", source)
             .order("created_at", desc=True).limit(limit).execute())
        return r.data or []

    def update(self, lead_id, fields):
        fields = {**fields, "updated_at": utcnow().isoformat()}
        return self.client.table("leads").update(fields).eq("id", lead_id).execute().data[0]


# ---------------------------------------------------------------- matching --

def match_listings(lead: dict, n: int = 3) -> list[dict]:
    """Finds up to `n` real listings for this lead using the same three-tier
    retrieval as bot.py. Returns [] if retrieval isn't available (e.g. no
    Supabase / vectorizer yet) — the lead is still stored and the agent alerted."""
    from supabase_retrieval import retrieve, get_supabase_client, load_vectorizer, embed_query

    query = lead.get("interest_text") or lead.get("category") or "property for sale"
    embedding = embed_query(query, load_vectorizer())
    results = retrieve(get_supabase_client(), embedding, category=lead.get("category"), n_results=n)
    return [{
        "project_name": r.project_name, "category": r.category, "location": r.location,
        "type_or_bhk": r.type_or_bhk, "price_raw": r.price_raw, "size_raw": r.size_raw,
        "status": r.status, "match_tier": r.match_tier,
    } for r in results]


# ----------------------------------------------------------------- drafting --

def _first_name(lead: dict) -> str:
    return (lead.get("name") or "").split(" ")[0].strip() or "there"


def _format_listing(m: dict) -> str:
    parts = [m.get("project_name"), m.get("type_or_bhk"), m.get("location")]
    line = ", ".join(p for p in parts if p)
    extras = [f"{label}: {m[key]}" for label, key in (("Price", "price_raw"), ("Size", "size_raw"), ("Status", "status")) if m.get(key)]
    return line + (f" ({'; '.join(extras)})" if extras else "")


def _template_body(lead: dict, matches: list[dict]) -> str:
    body = f"Hi {_first_name(lead)}, thanks for your enquiry with {BRAND_NAME}."
    if matches:
        body += " Based on what you're looking for, these may suit you:\n" + "\n".join(
            f"- {_format_listing(m)}" for m in matches)
    body += "\nWould you like to schedule a site visit? Tell us a day and time that works for you."
    return body


def _llm_body(lead: dict, matches: list[dict]) -> str:
    from anthropic import Anthropic

    listings = "\n".join(f"{i}. {_format_listing(m)}" for i, m in enumerate(matches, 1)) or "(none matched)"
    prompt = f"""Write the first WhatsApp reply from a real estate agency to a new enquiry.

Person: {_first_name(lead)}
What they asked for: {lead.get('interest_text') or '(not specified)'}

Listings we may suggest (the ONLY facts you may use):
{listings}

Rules:
- Max 80 words, friendly and plain, no emojis, no markdown.
- Use ONLY the facts above. Quote price/size text exactly as written; never convert, compute totals, or guess units.
- Do not invent amenities, returns, appreciation, possession dates, or discounts. Do not say "limited units" or create false urgency.
- If no listings matched, say we'll share suitable options shortly and ask one question about budget or area.
- End by inviting them to book a site visit.
- Output the message text only. Do NOT add a signature, RERA number, or opt-out line (added separately)."""
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=400, temperature=0.3,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


def draft_reply(lead: dict, matches: list[dict]) -> str:
    """First reply for the agent to send. The LLM (or template) writes only the
    body; the RERA line and opt-out line are appended by code so they can never
    be dropped, reworded, or invented by the model."""
    body = None
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            body = _llm_body(lead, matches)
        except Exception:
            logger.exception("LLM drafting failed; using template")
    body = body or _template_body(lead, matches)

    rera = os.getenv("AGENT_RERA_NO")
    footer = f"\n\n{BRAND_NAME} | {BRAND_CONTACT_NUMBER}"
    if rera:
        footer += f" | Agent RERA No: {rera}"
    footer += "\nReply STOP to opt out."
    return body + footer


# ----------------------------------------------------------------- alerting --

def build_alert_text(lead: dict, matches: list[dict], draft: str | None, note: str | None = None) -> str:
    lines = [f"NEW LEAD ({lead['source']})"]
    if note:
        lines.append(f"NOTE: {note}")
    lines += [
        f"Name: {lead.get('name') or '-'}",
        f"Phone: {lead.get('phone') or '-'}",
        f"Email: {lead.get('email') or '-'}",
        f"Looking for: {lead.get('interest_text') or '-'}",
    ]
    if matches:
        lines.append("Suggested listings:")
        lines += [f"  - {_format_listing(m)}" for m in matches]
    if draft:
        lines += ["", "Draft reply:", draft]
        if lead.get("phone_normalized"):
            link = f"https://wa.me/91{lead['phone_normalized']}?text={urllib.parse.quote(draft[:900])}"
            lines += ["", f"Tap to send on WhatsApp: {link}"]
    if draft and not os.getenv("AGENT_RERA_NO"):
        lines.append("\nWARNING: AGENT_RERA_NO is not set, so the draft has no RERA number.")
    return "\n".join(lines)


def notify_agent(text: str, dry_run: bool = False) -> bool:
    """Sends `text` to the agent on Telegram. Returns True only if actually sent."""
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("AGENT_TELEGRAM_CHAT_ID")
    if dry_run or not token or not chat_id:
        print("-" * 60 + "\n[ALERT NOT SENT - dry run or Telegram not configured]\n" + text + "\n" + "-" * 60)
        return False
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
        timeout=15,
    )
    response.raise_for_status()
    return True


# ------------------------------------------------------------- orchestrator --

def process_lead(lead: dict, store, *, matcher=None, drafter=None,
                 notifier=None, dry_run: bool = False) -> dict:
    """Runs the whole flow for one lead. Returns {"status": ..., "lead_id": ...}
    with status "created" or "duplicate"; failures in matching, drafting or
    alerting are logged and never lose the lead (it is stored first).
    matcher/drafter/notifier default to this module's functions, looked up at
    call time so tests can swap them."""
    matcher = matcher or match_listings
    drafter = drafter or draft_reply
    notifier = notifier or notify_agent
    # 1. Dedupe: same source id (webhook retry) or same person within the window.
    if lead.get("source_ref"):
        existing = store.find_by_source_ref(lead["source"], lead["source_ref"])
        if existing:
            return {"status": "duplicate", "lead_id": existing["id"], "reason": "same source_ref"}
    if lead.get("phone_normalized"):
        since = (utcnow() - timedelta(hours=DEDUPE_WINDOW_HOURS)).isoformat()
        existing = store.find_recent_by_phone(lead["phone_normalized"], since)
        if existing:
            return {"status": "duplicate", "lead_id": existing["id"], "reason": "same phone within window"}

    # 2. Decide what we can safely do with this lead.
    note = None
    if not lead.get("phone_normalized") and not lead.get("email"):
        status, note = "needs_review", "no valid phone or email - check the raw payload"
    elif not lead.get("consent"):
        status, note = "no_consent", "no recorded consent - do NOT send marketing messages"
    else:
        status = "new"

    # 3. Store first, so the lead is never lost if matching/drafting/alerting fails.
    try:
        row = store.insert({**lead, "status": status})
    except Exception:
        # Concurrent webhook retry hit the unique index: treat as duplicate.
        if lead.get("source_ref"):
            existing = store.find_by_source_ref(lead["source"], lead["source_ref"])
            if existing:
                return {"status": "duplicate", "lead_id": existing["id"], "reason": "insert race"}
        raise

    # 4. Match + draft (only for contactable leads with consent).
    matches, draft = [], None
    if status == "new":
        try:
            matches = matcher(lead)
        except Exception:
            logger.exception("listing match failed; continuing without matches")
        try:
            draft = drafter(lead, matches)
        except Exception:
            logger.exception("draft failed")
        store.update(row["id"], {"matched_projects": [m["project_name"] for m in matches], "draft_reply": draft})

    # 5. Alert the agent. notified_at stays null on failure so a retry job can find it.
    try:
        if notifier(build_alert_text(lead, matches, draft, note), dry_run=dry_run):
            store.update(row["id"], {"notified_at": utcnow().isoformat()})
    except Exception:
        logger.exception("agent alert failed; lead is stored with notified_at = null")

    return {"status": "created", "lead_id": row["id"], "lead_status": status, "matches": len(matches)}


# --------------------------------------------------------------------- demo --

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run a fake lead through the flow without any network.")
    parser.add_argument("--demo", action="store_true", required=True)
    parser.parse_args()

    fake_matches = lambda lead: [{"project_name": "Sample Square", "type_or_bhk": "Office", "location": "Abids",
                                  "price_raw": "9,500 per sft", "status": "Ready", "category": "commercials"}]
    demo = build_lead("meta_lead_ad", source_ref="demo-1", name="Ravi Kumar", phone="+91 99664 42577",
                      interest_text="commercial office space in Abids", consent=True,
                      consent_text="Submitted Meta lead form")
    print(process_lead(demo, InMemoryLeadStore(), matcher=fake_matches, dry_run=True))