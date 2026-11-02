'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Q = require('../lib/queue');
const { makeHandler: makeQueue } = require('../api/queue');
const { makeHandler: makeMark } = require('../api/mark');

const NOW = new Date('2026-11-02T06:00:00Z'); // 11:30 in India
const cfg = { ...Q.config({}), allowed: ['agent@example.com'], brandOk: new Set(['okay_sub']), brandName: 'Sample Realty' };

function row(id, over = {}) {
  return { id, name: 'user' + id, interest_text: 'Looking for a flat near Gachibowli', status: 'needs_review',
    reply_status: 'pending', replied_at: null, created_at: '2026-11-02T01:00:00Z', source: 'apify_reddit',
    raw_payload: { url: 'https://www.reddit.com/r/hyderabad/comments/x' + id + '/t/', intent_score: 5 }, ...over };
}

/** A tiny in-memory stand-in for the Supabase client. */
function fakeDb(rows, users = { good: 'agent@example.com', other: 'stranger@example.com' }) {
  const tables = { leads: rows };
  function builder(table) {
    const st = { filters: [], mode: 'select', fields: null, order: null, limit: null };
    const b = {
      select() { return b; },
      update(f) { st.mode = 'update'; st.fields = f; return b; },
      eq(c, v) { st.filters.push((r) => r[c] === v); return b; },
      gte(c, v) { st.filters.push((r) => r[c] && new Date(r[c]) >= new Date(v)); return b; },
      order(c, o) { st.order = [c, o.ascending]; return b; },
      limit(n) { st.limit = n; return b; },
      then(resolve) {
        let out = tables[table].filter((r) => st.filters.every((f) => f(r)));
        if (st.mode === 'update') { out.forEach((r) => Object.assign(r, st.fields)); out = out.map((r) => ({ id: r.id })); }
        else {
          if (st.order) out = [...out].sort((a, c) => (new Date(c[st.order[0]]) - new Date(a[st.order[0]])) * (st.order[1] ? -1 : 1));
          if (st.limit) out = out.slice(0, st.limit);
        }
        resolve({ data: out, error: null });
      },
    };
    return b;
  }
  return {
    from: builder,
    auth: { getUser: async (t) => (users[t] ? { data: { user: { email: users[t] } }, error: null } : { data: null, error: { message: 'bad' } }) },
  };
}
function call(handler, { method = 'GET', token = 'good', body, query } = {}) {
  return new Promise((resolve) => {
    const res = { code: 200, headers: {}, setHeader(k, v) { this.headers[k] = v; }, status(c) { this.code = c; return this; }, json(o) { resolve({ code: this.code, body: o }); } };
    handler({ method, headers: token ? { authorization: 'Bearer ' + token } : {}, body, query }, res);
  });
}

test('draft is advice-only unless the subreddit allows the business name', () => {
  assert.ok(!Q.draftReply('plot', false, 'Sample Realty').includes('Sample Realty'));
  assert.ok(Q.draftReply('plot', true, 'Sample Realty').includes('Sample Realty'));
  assert.ok(Q.draftReply('plot near Shamshabad', false, 'x').startsWith('For plots'));
  assert.ok(Q.draftReply('open shop', false, 'x').startsWith('For flats'));
});

test('queue shows highest intent first and skips one author twice', () => {
  const rows = [row(1, { raw_payload: { url: 'https://reddit.com/r/a/comments/1/', intent_score: 4 } }),
    row(2, { raw_payload: { url: 'https://reddit.com/r/a/comments/2/', intent_score: 8 } }),
    row(3, { name: 'user2', raw_payload: { url: 'https://reddit.com/r/a/comments/3/', intent_score: 9 } })];
  const out = Q.nextPosts(rows, { now: NOW, cfg });
  assert.deepEqual(out.posts.map((p) => p.id), [3, 1]); // user2's second (lower) post is dropped
});

test('daily limit stops the queue', () => {
  const done = Array.from({ length: 5 }, (_, i) => row(100 + i, { reply_status: 'replied', replied_at: '2026-11-02T02:00:00Z', name: 'd' + i }));
  const out = Q.nextPosts([...done, row(1)], { now: NOW, cfg });
  assert.equal(out.posts.length, 0);
  assert.equal(out.left, 0);
});

test('an author answered recently is skipped, an old one is not', () => {
  const rows = [row(1, { name: 'recent', reply_status: 'replied', replied_at: '2026-10-30T02:00:00Z' }),
    row(2, { name: 'recent' }), row(3, { name: 'old', reply_status: 'replied', replied_at: '2026-09-01T02:00:00Z' }), row(4, { name: 'old' })];
  assert.deepEqual(Q.nextPosts(rows, { now: NOW, cfg }).posts.map((p) => p.id), [4]);
});

test('queue API: needs sign-in, rejects strangers, returns posts', async () => {
  const h = makeQueue(() => fakeDb([row(1), row(2)]), () => cfg);
  assert.equal((await call(h, { token: null })).code, 401);
  assert.equal((await call(h, { token: 'other' })).code, 403);
  assert.equal((await call(h, { token: 'nope' })).code, 401);
  const ok = await call(h);
  assert.equal(ok.code, 200);
  assert.equal(ok.body.posts.length, 2);
  assert.equal((await call(h, { method: 'POST' })).code, 405);
});

test('nobody can sign in when ALLOWED_EMAILS is empty', async () => {
  const h = makeQueue(() => fakeDb([row(1)]), () => ({ ...cfg, allowed: [] }));
  assert.equal((await call(h)).code, 500);
});

test('mark API: records a reply, validates input, enforces the daily cap', async () => {
  const rows = [row(1), row(2)];
  const h = makeMark(() => fakeDb(rows), () => cfg, () => NOW);
  assert.equal((await call(h, { method: 'POST', token: null, body: { id: 1, action: 'replied' } })).code, 401);
  assert.equal((await call(h, { method: 'POST', body: { id: 1, action: 'delete' } })).code, 400);
  assert.equal((await call(h, { method: 'POST', body: { action: 'replied' } })).code, 400);
  assert.equal((await call(h, { method: 'POST', body: { id: 999, action: 'skipped' } })).code, 404);
  const ok = await call(h, { method: 'POST', body: { id: 1, action: 'replied', note: 'posted' } });
  assert.equal(ok.code, 200);
  assert.equal(rows[0].reply_status, 'replied');
  assert.ok(rows[0].replied_at);
  const skip = await call(h, { method: 'POST', body: { id: 2, action: 'skipped' } });
  assert.equal(skip.code, 200);
  assert.equal(rows[1].replied_at, null); // skipping leaves no "replied" time

  const full = Array.from({ length: 5 }, (_, i) => row(200 + i, { reply_status: 'replied', replied_at: '2026-11-02T02:00:00Z', name: 'f' + i }));
  const h2 = makeMark(() => fakeDb([...full, row(1)]), () => cfg, () => NOW);
  assert.equal((await call(h2, { method: 'POST', body: { id: 1, action: 'replied' } })).code, 409);
  assert.equal((await call(h2, { method: 'POST', body: { id: 1, action: 'skipped' } })).code, 200);
});
