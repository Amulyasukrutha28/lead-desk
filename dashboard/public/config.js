// Edit these two values. Both are safe to be public:
// the publishable key can only sign in, and it cannot read the leads table.
// NEVER put the secret key in this file.
window.LEAD_DESK = {
  supabaseUrl: "https://YOUR-PROJECT-ID.supabase.co",
  supabasePublishableKey: "sb_publishable_YOUR_KEY",
  refreshSeconds: 60,
};
