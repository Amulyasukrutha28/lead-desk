# Lead Desk: live setup

What runs where
- GitHub Actions: every 3 hours, `apify_source.py` searches Reddit and saves buyer posts to Supabase.
- Vercel: the private dashboard (`dashboard/`). It shows saved posts with draft replies.
- A person copies a draft, posts it on Reddit by hand, and presses "I replied". Nothing is posted automatically.

## 1. Supabase (10 minutes)
1. Make sure `leads_only.sql` has been run (the `leads` table exists, row level security on).
2. Authentication -> Users -> Add user: create the agent's login (email + password).
3. Authentication -> Sign In / Providers -> turn OFF "Allow new users to sign up".
4. Project Settings -> API Keys: you need the project URL, the publishable key and the secret key.

## 2. Put it in a private GitHub repository
Repository layout:
```
lead_capture/        your existing folder (no .env file in it!)
dashboard/           from this package
.github/workflows/scrape.yml
```
Make the repository PRIVATE. Add a `.gitignore` containing `.env`, `node_modules/`.
Never commit `.env`. If you ever do, rotate every key in it.

## 3. Scraper secrets (GitHub)
Settings -> Secrets and variables -> Actions -> New repository secret. Add one per value in your local `.env`
(APIFY_TOKEN, the Supabase URL and secret key under the same names your `.env` uses, the Telegram ones).
If a name differs from the ones in `scrape.yml`, edit the `env:` block to match.
Test it: Actions tab -> "Search Reddit for buyer posts" -> Run workflow.

## 4. Dashboard (Vercel)
1. Edit `dashboard/public/config.js`: the Supabase URL and the PUBLISHABLE key (never the secret key).
2. Vercel -> Add New -> Project -> import the repo -> set Root Directory to `dashboard`.
3. Environment Variables (see `dashboard/.env.example`): SUPABASE_URL, SUPABASE_SECRET_KEY, ALLOWED_EMAILS,
   APIFY_SOURCE_NAME, REDDIT_REPLIES_PER_DAY, AUTHOR_COOLDOWN_DAYS, REDDIT_BRAND_OK_SUBS, BRAND_NAME.
4. Deploy, open the link, sign in with the agent's email and password.

## Tests
`cd dashboard && npm install && npm test` (7 tests, no network needed).

## Limits to know
- GitHub's scheduler can start a run a few minutes late, and pauses schedules in repos with no activity for 60 days.
- Scraping Reddit through Apify sometimes returns nothing ("temporary access issue"). The next run tries again.
- The dashboard refreshes every minute and does not replace a draft you are editing.
