-- ===========================================================================
--  CRT Study - returning-participant lookup
--  Run this in the Supabase dashboard:  SQL Editor -> New query -> Run.
--  Safe to run more than once.
-- ===========================================================================
--
--  Why this exists
--  ---------------
--  The sign-in form now offers to recognise a name it has seen before: when
--  a participant types a name and clicks away from the field, the app asks
--  "is this the same <name> as before?" and, if so, prefills their role and
--  age group and lets them continue in one click.
--
--  Two things have to be true for that to work:
--
--  1. It also fixes a real bug: two different people can share initials
--     (two "Hiruni" medical students, say). Without this, the second one to
--     sign in would be treated as the SAME participant as the first — and
--     since clips already marked are never re-shown to "that name", the
--     second Hiruni would see nothing at all. The lookup this file adds
--     returns the next free "(2)", "(3)"... suffix so the app can offer her
--     a name that is still recognisably hers, but never collides with the
--     first Hiruni's.
--
--  2. Participants submit with the site's PUBLIC key, and that key has no
--     read access to `annotations` (see supabase_hardening.sql) — on
--     purpose, so nobody can browse other people's marks. This lookup has
--     to run with elevated privilege to answer "does this name exist" at
--     all, so it is a narrow SECURITY DEFINER function that returns only
--     what the sign-in form needs (a name, a role, an age group, a count) —
--     never a full row, never anybody else's timing data.
--
--  The client degrades gracefully if this file hasn't been run yet: the
--  RPC call 404s, the app catches that, and the name field behaves exactly
--  as it did before this feature existed. Nothing breaks either way.


-- ---------------------------------------------------------------------------
-- "Have we seen this name before, and what's the next free variant of it?"
-- ---------------------------------------------------------------------------
-- Matching is case- and whitespace-insensitive ("Ann", "ann ", "ANN" are the
-- same lookup), consistent with how the app already normalises names on the
-- client (nameKey() in index.html). A name that already carries a "(2)",
-- "(3)"... suffix is treated as its own separate identity, not folded back
-- into the base name — that's the whole point of the suffix.
create or replace function public.crt_lookup_participant(p_name text)
returns table (
  exists_flag   boolean,
  canonical_name text,
  role           text,
  age_group      text,
  next_suffix    int
)
language sql
security definer
set search_path = public
stable
as $$
  with key as (
    select regexp_replace(lower(trim(p_name)), '\s+', ' ', 'g') as k
  ),
  -- Rows whose display_name IS the base name (no trailing "(N)").
  base_match as (
    select a.display_name, a.role, a.age_group, a.server_received_at
      from public.annotations a, key
     where regexp_replace(lower(trim(a.display_name)), '\s+', ' ', 'g') = key.k
  ),
  latest as (
    select display_name, role, age_group
      from base_match
     order by server_received_at desc
     limit 1
  ),
  -- The highest "(N)" already used for THIS base name, so a second person
  -- with the same name is offered the next free one.
  suffixes as (
    select (regexp_match(a.display_name, '\((\d+)\)\s*$'))[1]::int as n
      from public.annotations a, key
     where a.display_name ~ '\(\d+\)\s*$'
       and regexp_replace(
             lower(trim(regexp_replace(a.display_name, '\s*\(\d+\)\s*$', ''))),
             '\s+', ' ', 'g'
           ) = key.k
  )
  select
    (select display_name from latest) is not null                as exists_flag,
    (select display_name from latest)                             as canonical_name,
    (select role from latest)                                     as role,
    (select age_group from latest)                                as age_group,
    greatest(2, coalesce((select max(n) from suffixes), 1) + 1)    as next_suffix;
$$;

revoke all on function public.crt_lookup_participant(text) from public;
grant execute on function public.crt_lookup_participant(text) to anon, authenticated;


-- ---------------------------------------------------------------------------
-- Check it worked
-- ---------------------------------------------------------------------------
-- Run these on their own afterwards, swapping in a name that has already
-- submitted at least one annotation.
--
--   select * from public.crt_lookup_participant('ann');
--   -- exists_flag should be true, canonical_name/role/age_group should be
--   -- filled in from Ann's most recent submission.
--
--   select * from public.crt_lookup_participant('a name nobody has used');
--   -- exists_flag should be false, everything else null, next_suffix = 2.
