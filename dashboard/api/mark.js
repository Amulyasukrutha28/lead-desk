'use strict';
const { adminClient, requireUser } = require('../lib/auth');
const { config, VALID_MARKS, istDayStart } = require('../lib/queue');

/** POST /api/mark {id, action, note} : records what the agent did by hand. It posts nothing to Reddit. */
function makeHandler(getDb = adminClient, getCfg = config, clock = () => new Date()) {
  return async function handler(req, res) {
    res.setHeader('Cache-Control', 'no-store');
    if (req.method !== 'POST') return res.status(405).json({ error: 'Use POST.' });
    try {
      const cfg = getCfg();
      const db = getDb();
      const who = await requireUser(req, db, cfg.allowed);
      if (who.error) return res.status(who.status).json({ error: who.error });

      const body = req.body || {};
      const id = Number(body.id);
      const action = body.action;
      const note = typeof body.note === 'string' ? body.note.slice(0, 500) : null;
      if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Missing post id.' });
      if (!VALID_MARKS.includes(action)) return res.status(400).json({ error: 'Action must be one of: ' + VALID_MARKS.join(', ') + '.' });

      const now = clock();
      if (action === 'replied') {
        const { data: today, error: e1 } = await db.from('leads').select('id')
          .eq('source', cfg.source).eq('reply_status', 'replied').gte('replied_at', istDayStart(now));
        if (e1) return res.status(502).json({ error: 'Could not check today\'s count: ' + e1.message });
        if ((today || []).length >= cfg.perDay) {
          return res.status(409).json({ error: `Daily limit of ${cfg.perDay} replies reached.` });
        }
      }

      const fields = { reply_status: action, reply_note: note, updated_at: now.toISOString() };
      if (action === 'replied' || action === 'they_replied') fields.replied_at = now.toISOString();

      const { data, error } = await db.from('leads').update(fields).eq('id', id).eq('source', cfg.source).select('id');
      if (error) return res.status(502).json({ error: 'Could not save: ' + error.message });
      if (!data || !data.length) return res.status(404).json({ error: 'That post was not found.' });
      return res.status(200).json({ ok: true, id, action });
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  };
}

module.exports = makeHandler();
module.exports.makeHandler = makeHandler;
