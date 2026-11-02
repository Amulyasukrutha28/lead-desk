'use strict';
const { adminClient, requireUser } = require('../lib/auth');
const { config, nextPosts } = require('../lib/queue');

/** GET /api/queue : the posts to answer now, each with a draft reply. Read-only. */
function makeHandler(getDb = adminClient, getCfg = config) {
  return async function handler(req, res) {
    res.setHeader('Cache-Control', 'no-store');
    if (req.method !== 'GET') return res.status(405).json({ error: 'Use GET.' });
    try {
      const cfg = getCfg();
      const db = getDb();
      const who = await requireUser(req, db, cfg.allowed);
      if (who.error) return res.status(who.status).json({ error: who.error });

      const { data, error } = await db.from('leads')
        .select('id,name,interest_text,status,reply_status,replied_at,created_at,raw_payload')
        .eq('source', cfg.source).order('created_at', { ascending: false }).limit(500);
      if (error) return res.status(502).json({ error: 'Could not read the leads table: ' + error.message });

      const n = Math.min(Math.max(parseInt((req.query || {}).n || '5', 10) || 5, 1), 10);
      const result = nextPosts(data || [], { n, cfg });
      const pending = (data || []).filter((r) => (r.reply_status || 'pending') === 'pending' && r.status === 'needs_review').length;
      return res.status(200).json({ ...result, pending, perDay: cfg.perDay });
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  };
}

module.exports = makeHandler();
module.exports.makeHandler = makeHandler;
