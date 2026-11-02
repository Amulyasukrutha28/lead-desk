'use strict';
const { createClient } = require('@supabase/supabase-js');

/** Server-side client. Uses the SECRET key, so it must only ever run in these API functions. */
function adminClient(env = process.env) {
  if (!env.SUPABASE_URL || !env.SUPABASE_SECRET_KEY) {
    throw new Error('Set SUPABASE_URL and SUPABASE_SECRET_KEY in the Vercel environment variables.');
  }
  return createClient(env.SUPABASE_URL, env.SUPABASE_SECRET_KEY, { auth: { persistSession: false, autoRefreshToken: false } });
}

/**
 * Checks the signed-in person. Returns their email, or null.
 * Only emails listed in ALLOWED_EMAILS get in, so a stray account can never read leads.
 */
async function requireUser(req, db, allowed) {
  if (!allowed.length) return { error: 'ALLOWED_EMAILS is not set, so nobody can sign in yet.', status: 500 };
  const header = req.headers.authorization || req.headers.Authorization || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : '';
  if (!token) return { error: 'Sign in to continue.', status: 401 };
  const { data, error } = await db.auth.getUser(token);
  const email = data && data.user && data.user.email ? data.user.email.toLowerCase() : null;
  if (error || !email) return { error: 'Your session has ended. Sign in again.', status: 401 };
  if (!allowed.includes(email)) return { error: 'This account does not have access.', status: 403 };
  return { email };
}

module.exports = { adminClient, requireUser };
