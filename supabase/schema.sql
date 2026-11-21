-- STEALTHBOT online panel: Supabase schema (run once; safe to run again).
--
-- Design: the bot on your PC never accepts a connection from the internet. It calls OUT to Supabase:
--   * it publishes a status snapshot into bot_status (one row),
--   * it reads commands that the web panel inserted into bot_commands, runs them through its own local API, and writes the result.
-- Who may do what is decided here by Row Level Security (RLS), on top of Supabase Auth:
--   admin     : read the status, insert any allowed command, read all commands
--   streamer  : read the status, insert ONLY the 'say' command (hold-to-speak), read their own commands
--   bot       : the bot's own login: write the status, claim/finish commands, read profiles. Nothing else.
--   anon / signed-in users without a profile: nothing at all.
-- Roles live in public.profiles, which nobody can change through the API (no insert/update/delete policy): they are set by
-- the project owner (SQL editor or the management API), never by a user.

create extension if not exists pgcrypto;

-- ------------------------------------------------------------------ roles
create table if not exists public.profiles (
  user_id      uuid primary key references auth.users (id) on delete cascade,
  role         text not null check (role in ('admin', 'streamer', 'bot')),
  display_name text,
  created_at   timestamptz not null default now()
);
alter table public.profiles enable row level security;

-- The role of the current user. SECURITY DEFINER so the policies below can read profiles without recursion.
create or replace function public.current_role_name() returns text
language sql stable security definer set search_path = public as $$
  select role from public.profiles where user_id = auth.uid()
$$;
revoke all on function public.current_role_name() from public, anon;
grant execute on function public.current_role_name() to authenticated;

drop policy if exists profiles_read_own on public.profiles;
create policy profiles_read_own on public.profiles for select to authenticated using (user_id = auth.uid());
drop policy if exists profiles_bot_read on public.profiles;
create policy profiles_bot_read on public.profiles for select to authenticated using (public.current_role_name() = 'bot');

-- ------------------------------------------------------------------ the status the bot publishes
create table if not exists public.bot_status (
  id         int primary key default 1 check (id = 1),
  data       jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);
alter table public.bot_status enable row level security;

drop policy if exists status_read on public.bot_status;
create policy status_read on public.bot_status for select to authenticated
  using (public.current_role_name() in ('admin', 'streamer', 'bot'));
drop policy if exists status_bot_insert on public.bot_status;
create policy status_bot_insert on public.bot_status for insert to authenticated
  with check (public.current_role_name() = 'bot');
drop policy if exists status_bot_update on public.bot_status;
create policy status_bot_update on public.bot_status for update to authenticated
  using (public.current_role_name() = 'bot') with check (public.current_role_name() = 'bot');

-- ------------------------------------------------------------------ the commands the panel sends
create table if not exists public.bot_commands (
  id         bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  created_by uuid not null default auth.uid() references auth.users (id) on delete cascade,
  kind       text not null check (kind in (
               'pause', 'resume', 'set_approval', 'set_ai', 'set_proactive', 'approve', 'reject',
               'select_stream', 'add_stream', 'update_stream', 'remove_stream', 'stream_settings',
               'set_topic', 'say')),
  payload    jsonb not null default '{}'::jsonb check (octet_length(payload::text) <= 4000),
  status     text not null default 'pending' check (status in ('pending', 'running', 'done', 'failed', 'rejected')),
  result     jsonb,
  handled_at timestamptz
);
create index if not exists bot_commands_pending on public.bot_commands (id) where status = 'pending';
alter table public.bot_commands enable row level security;

-- Insert: an admin may send any command; a streamer only 'say'; always as themselves and always 'pending'.
drop policy if exists commands_insert on public.bot_commands;
create policy commands_insert on public.bot_commands for insert to authenticated
  with check (
    created_by = auth.uid() and status = 'pending' and result is null and handled_at is null and (
      public.current_role_name() = 'admin'
      or (public.current_role_name() = 'streamer' and kind = 'say')
    )
  );
-- Read: admin and bot see everything, a streamer sees their own.
drop policy if exists commands_read on public.bot_commands;
create policy commands_read on public.bot_commands for select to authenticated
  using (public.current_role_name() in ('admin', 'bot') or created_by = auth.uid());
-- Only the bot changes or deletes commands (and only the three result columns: see the grants below).
drop policy if exists commands_bot_update on public.bot_commands;
create policy commands_bot_update on public.bot_commands for update to authenticated
  using (public.current_role_name() = 'bot') with check (public.current_role_name() = 'bot');
drop policy if exists commands_bot_delete on public.bot_commands;
create policy commands_bot_delete on public.bot_commands for delete to authenticated
  using (public.current_role_name() = 'bot');

-- ------------------------------------------------------------------ table privileges (defence in depth next to RLS)
revoke all on public.profiles, public.bot_status, public.bot_commands from anon, authenticated;
grant select on public.profiles to authenticated;
grant select, insert, update on public.bot_status to authenticated;
grant select on public.bot_commands to authenticated;
grant insert (kind, payload) on public.bot_commands to authenticated;      -- created_by/status default; nothing else is writable
grant update (status, result, handled_at) on public.bot_commands to authenticated;
grant delete on public.bot_commands to authenticated;

-- ------------------------------------------------------------------ live updates in the panel (RLS still applies)
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    begin alter publication supabase_realtime add table public.bot_status; exception when duplicate_object then null; end;
    begin alter publication supabase_realtime add table public.bot_commands; exception when duplicate_object then null; end;
  end if;
end $$;
