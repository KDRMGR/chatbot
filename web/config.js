/* Public settings of the online panel. Both values are PUBLIC by design: the project address and the publishable (anon) key.
   Never put a secret / service_role key here: this file is sent to every visitor. What a signed-in user may do is decided by the
   database's row-level-security policies (supabase/schema.sql), not by this key. */
window.STEALTHBOT_CONFIG = {
  url: "https://dwzlmwwygpszzbiwleya.supabase.co",
  key: "sb_publishable_gZVMQjebewz7RrwF7AsP4w_XMmNqSPy",
};
