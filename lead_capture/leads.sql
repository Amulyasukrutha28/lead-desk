-- leads_only.sql
-- Run once in Supabase -> SQL Editor. Creates the `leads` table used by
-- leads.py, apify_source.py and reddit_review.py.
-- If a half-made `leads` table already exists, rename it first:
--   alter table public.leads rename to leads_empty_backup;

create table if not exists public.leads (
    id               bigint generated always as identity primary key,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz,
    source           text not null,
    source_ref       text,
    name             text,
    phone            text,
    phone_normalized text,
    email            text,
    interest_text    text,
    category         text,
    consent          boolean not null default false,
    consent_at       timestamptz,
    consent_text     text,
    raw_payload      jsonb,
    status           text not null default 'new',
    matched_projects text[],
    draft_reply      text,
    notified_at      timestamptz,
    reply_status     text not null default 'pending'
        check (reply_status in ('pending','replied','they_replied','shared_contact','skipped')),
    replied_at       timestamptz,
    reply_note       text
);

-- the same post can never be saved twice
create unique index if not exists idx_leads_source_ref
    on public.leads (source, source_ref) where source_ref is not null;
create index if not exists idx_leads_phone on public.leads (phone_normalized);
create index if not exists idx_leads_reply_status on public.leads (reply_status);

-- leads hold names and phone numbers: block the public (anon) key.
-- Scripts using the service role key still work.
alter table public.leads enable row level security;

-- lets the API see the new table straight away
notify pgrst, 'reload schema';
