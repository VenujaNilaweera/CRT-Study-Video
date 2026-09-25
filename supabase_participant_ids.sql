-- ===========================================================================
--  CRT Study - a stable Study ID for every participant
--  Run this in the Supabase dashboard:  SQL Editor -> New query -> Run.
--  Safe to run more than once, and safe whether or not the older
--  "6. Participant identity" block (from the main branch's
--  supabase_balancing.sql) was ever run.
-- ===========================================================================
--
--  Why this exists
--  ---------------
--  Marks are grouped by the name a participant types. For the analysis that is
--  not good enough: a name is personal data, and a typo or a capital letter
--  can split one person into two. So every participant gets a separate,
--  opaque Study ID (a uuid plus a short "P-7F31A2C9" code), and every mark
--  carries it in annotations.participant_id.
--
--  HOW THE ID IS ASSIGNED - entirely in the database
--  ------------------------------------------------
--  A trigger on annotations fills participant_id on every insert, from the
--  display name. The site does not have to send anything new, so:
--    * it works for the live site as it is today,
--    * it works for marks still sitting in someone's offline queue,
--    * nobody can forge or pick another person's ID from the browser.
--
--  One name = one person. That holds because the sign-in form already keeps
--  names unique: a second "Ann" is offered "Ann (2)" (see
--  supabase_returning_users.sql), and a returning Ann confirms she is the same
--  Ann. Names are matched ignoring case and extra spaces, exactly like that
--  lookup, so "ann " and "Ann" are the same participant.
--
--  Every mark ALREADY collected is back-filled in step 4, so the analysis gets
--  a Study ID on every row, old and new.
--
--  ALSO FIXES: the older main-branch block dropped crt_seen_videos(text),
--  which is what the live site calls to avoid re-showing a clip someone has
--  already marked. Step 6 puts it back.
-- ===========================================================================

create extension if not exists pgcrypto;


-- ---------------------------------------------------------------------------
-- 1. The participants table
-- ---------------------------------------------------------------------------
create table if not exists public.study_participants (
  id               uuid primary key default gen_random_uuid(),
  participant_code text unique not null
                   default ('P-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8))),
  display_name     text,
  role             text,
  age_group        text,
  created_at       timestamptz not null default now()
);

-- The normalised name this participant is known by. Unique, so two marks
-- arriving at the same moment for a brand-new name still land on ONE id.
alter table public.study_participants add column if not exists name_key text;

-- Rows made by the older block have no name_key yet. Give it to the earliest
-- row per name only; any later duplicates keep null rather than break the
-- unique index below.
update public.study_participants p
   set name_key = regexp_replace(lower(trim(p.display_name)), '\s+', ' ', 'g')
 where p.name_key is null
   and p.display_name is not null
   and p.id in (select distinct on (regexp_replace(lower(trim(display_name)), '\s+', ' ', 'g')) id
                  from public.study_participants
                 where display_name is not null
                 order by regexp_replace(lower(trim(display_name)), '\s+', ' ', 'g'), created_at, id)
   and not exists (select 1 from public.study_participants q
                    where q.name_key = regexp_replace(lower(trim(p.display_name)), '\s+', ' ', 'g'));

create unique index if not exists study_participants_name_key_idx
  on public.study_participants (name_key);

-- Nobody but the service key reads or writes this table directly. The older
-- block let the public key insert rows at will; that is closed here.
alter table public.study_participants enable row level security;
drop policy if exists "anon can insert participant" on public.study_participants;
revoke all on public.study_participants from anon, authenticated;


-- ---------------------------------------------------------------------------
-- 2. The link from every mark to its participant
-- ---------------------------------------------------------------------------
alter table public.annotations add column if not exists participant_id uuid;

alter table public.annotations drop constraint if exists annotations_participant_fk;
alter table public.annotations
  add constraint annotations_participant_fk
  foreign key (participant_id) references public.study_participants(id)
  on delete set null;

create index if not exists annotations_participant_id_idx
  on public.annotations (participant_id);


-- ---------------------------------------------------------------------------
-- 3. Name -> Study ID (get it, or create it the first time)
-- ---------------------------------------------------------------------------
create or replace function public.crt_participant_for(p_name text, p_role text, p_age text)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  k   text := regexp_replace(lower(trim(coalesce(p_name, ''))), '\s+', ' ', 'g');
  pid uuid;
begin
  if k = '' then
    return null;
  end if;
  select id into pid from public.study_participants where name_key = k;
  if pid is null then
    insert into public.study_participants (name_key, display_name, role, age_group)
    values (k, trim(p_name), nullif(trim(p_role), ''), nullif(trim(p_age), ''))
    on conflict (name_key) do nothing
    returning id into pid;
    if pid is null then       -- someone else created it a moment ago
      select id into pid from public.study_participants where name_key = k;
    end if;
  end if;
  return pid;
end;
$$;

revoke all on function public.crt_participant_for(text, text, text) from public, anon, authenticated;

-- Every new mark gets its participant_id here, before it is stored. Whatever
-- the browser sent in that column is ignored: the ID always follows the name.
create or replace function public.crt_assign_participant()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  new.participant_id := public.crt_participant_for(new.display_name, new.role, new.age_group);
  return new;
end;
$$;

drop trigger if exists crt_assign_participant on public.annotations;
create trigger crt_assign_participant
  before insert on public.annotations
  for each row execute function public.crt_assign_participant();


-- ---------------------------------------------------------------------------
-- 4. Back-fill every mark already collected
-- ---------------------------------------------------------------------------
-- Oldest first, so each participant's stored role/age are the ones they gave
-- on their first mark.
do $$
declare r record;
begin
  for r in
    select distinct on (regexp_replace(lower(trim(display_name)), '\s+', ' ', 'g'))
           display_name, role, age_group
      from public.annotations
     where display_name is not null and trim(display_name) <> ''
     order by regexp_replace(lower(trim(display_name)), '\s+', ' ', 'g'),
              server_received_at nulls last
  loop
    perform public.crt_participant_for(r.display_name, r.role, r.age_group);
  end loop;
end $$;

-- Re-point every mark at the participant its name belongs to (this also
-- corrects marks the older block linked to a per-sign-in duplicate).
update public.annotations a
   set participant_id = p.id
  from public.study_participants p
 where p.name_key = regexp_replace(lower(trim(a.display_name)), '\s+', ' ', 'g')
   and a.participant_id is distinct from p.id;


-- ---------------------------------------------------------------------------
-- 5. Remove the older block's browser-facing pieces
-- ---------------------------------------------------------------------------
-- Nothing on the live site calls these. crt_register_participant let the
-- public key create participant rows without limit; the trigger above
-- replaces it.
drop function if exists public.crt_register_participant(text, text, text, text);
drop function if exists public.crt_register_participant(text, text, text);
drop function if exists public.crt_seen_videos(uuid);


-- ---------------------------------------------------------------------------
-- 6. Put back the helper the live site calls
-- ---------------------------------------------------------------------------
-- Identical to supabase_balancing.sql step 4.
create or replace function public.crt_seen_videos(p_name text)
returns table (video_id uuid)
language sql
security definer
set search_path = public
stable
as $$
  select distinct a.video_id
    from public.annotations a
   where a.video_id is not null
     and a.display_name is not distinct from p_name;
$$;

revoke all on function public.crt_seen_videos(text) from public;
grant execute on function public.crt_seen_videos(text) to anon, authenticated;


-- ---------------------------------------------------------------------------
-- 7. Check it worked (run these on their own afterwards)
-- ---------------------------------------------------------------------------
-- Every mark has a Study ID - expect 0:
--   select count(*) from public.annotations where participant_id is null;
--
-- One row per participant, with how many marks each has:
--   select p.participant_code, p.display_name, p.role, count(a.*) as marks
--     from public.study_participants p
--     left join public.annotations a on a.participant_id = p.id
--    group by 1, 2, 3 order by marks desc;
