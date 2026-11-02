'use strict';
/*
 * The review-queue rules, ported from reddit_review.py so the dashboard and the
 * command-line tool behave the same way. Nothing here sends anything anywhere:
 * it only decides which saved posts to show, and writes a draft for a person to edit.
 */

const TIPS = {
  plot: "For plots, check the layout is HMDA/DTCP approved, ask for the LP/RERA number, and verify the title chain with a lawyer before paying any token.",
  flat: "For flats, check the RERA registration and approvals, the builder's past deliveries, and compare the carpet area (not super built-up) when you compare prices.",
  villa: "For villas, check whether it is a gated community with RERA registration, the plot share in the sale deed, and the maintenance charges.",
  commercial: "For commercial space, check the occupancy certificate, parking ratio and current rental yield.",
};
const VALID_MARKS = ['replied', 'they_replied', 'skipped'];

function list(value) {
  return String(value || '').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);
}

function config(env = process.env) {
  return {
    source: env.APIFY_SOURCE_NAME || 'apify_reddit',
    perDay: parseInt(env.REDDIT_REPLIES_PER_DAY || '5', 10),
    cooldownDays: parseInt(env.AUTHOR_COOLDOWN_DAYS || '14', 10),
    brandOk: new Set(list(env.REDDIT_BRAND_OK_SUBS)),
    brandName: env.BRAND_NAME || 'our team',
    allowed: list(env.ALLOWED_EMAILS),
  };
}

function subredditOf(url) {
  const m = /\/r\/([A-Za-z0-9_]+)\//.exec(url || '');
  return m ? m[1].toLowerCase() : null;
}

function tipFor(text) {
  const t = String(text || '').toLowerCase();
  for (const key of ['plot', 'villa', 'commercial']) if (t.includes(key)) return TIPS[key];
  return TIPS.flat;
}

function draftReply(text, brandOk, brandName) {
  let reply = tipFor(text) + ' Also, share your budget and preferred area and people here can help you narrow it down.';
  if (brandOk) {
    reply += ` I'm a property dealer in Hyderabad (${brandName}) - if you'd like, message me and I'll share a few options in your budget, no obligation.`;
  }
  return reply;
}

/** The calendar day in India for a timestamp, as YYYY-MM-DD. */
function istDay(ts) {
  return new Date(ts).toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
}

/** The moment today started in India, as an ISO string. */
function istDayStart(now) {
  return new Date(istDay(now) + 'T00:00:00+05:30').toISOString();
}

function repliedToday(rows, now) {
  const today = istDay(now);
  return rows.filter((r) => r.reply_status === 'replied' && r.replied_at && istDay(r.replied_at) === today).length;
}

function recentAuthors(rows, now, cooldownDays) {
  const cutoff = new Date(now).getTime() - cooldownDays * 86400000;
  const out = new Set();
  for (const r of rows) {
    if (['replied', 'they_replied', 'shared_contact'].includes(r.reply_status) && r.name && r.replied_at &&
        new Date(r.replied_at).getTime() >= cutoff) out.add(r.name.toLowerCase());
  }
  return out;
}

/** Posts worth answering now: highest intent first, daily cap and author cooldown applied. */
function nextPosts(rows, { now = new Date(), n = 5, cfg = config() } = {}) {
  const done = repliedToday(rows, now);
  const left = Math.max(0, cfg.perDay - done);
  if (left === 0) return { posts: [], left: 0, reason: `Daily limit of ${cfg.perDay} replies reached. Come back tomorrow.` };

  const blocked = recentAuthors(rows, now, cfg.cooldownDays);
  const seenAuthors = new Set();
  const candidates = rows
    .filter((r) => (r.reply_status || 'pending') === 'pending' && r.status === 'needs_review')
    .sort((a, b) => ((b.raw_payload || {}).intent_score || 0) - ((a.raw_payload || {}).intent_score || 0) ||
                    new Date(b.created_at) - new Date(a.created_at));

  const posts = [];
  for (const r of candidates) {
    const author = (r.name || '').toLowerCase();
    if (author && (blocked.has(author) || seenAuthors.has(author))) continue;
    if (author) seenAuthors.add(author);
    const url = (r.raw_payload || {}).url || null;
    const sub = subredditOf(url);
    const brandOk = !!sub && cfg.brandOk.has(sub);
    posts.push({
      id: r.id, url, subreddit: sub, author: r.name || null,
      post: String(r.interest_text || '').slice(0, 600),
      score: (r.raw_payload || {}).intent_score ?? null,
      brandOk, draft: draftReply(r.interest_text, brandOk, cfg.brandName),
      createdAt: r.created_at,
    });
    if (posts.length >= Math.min(n, left)) break;
  }
  return { posts, left, reason: null };
}

module.exports = { TIPS, VALID_MARKS, config, subredditOf, tipFor, draftReply, istDay, istDayStart, repliedToday, recentAuthors, nextPosts };
