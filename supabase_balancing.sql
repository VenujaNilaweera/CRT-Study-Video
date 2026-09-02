-- ===========================================================================
--  CRT Study - even annotation coverage
--  Run this in the Supabase dashboard:  SQL Editor -> New query -> Run.
--  Safe to run more than once.
-- ===========================================================================
--
--  Why this exists
--  ---------------
--  Every participant used to start on the same clip, so the opening videos
--  collected all the marks while the rest collected none. To hand each viewer
--  the LEAST-annotated clips first, the app has to know how many marks each
--  clip already carries.
--
--  It must not learn that by reading the annotations table: participants
--  submit with the site's public key, and letting that key read annotations
--  would expose everyone's marks (and their names) to anyone who opened the
--  page. So the figure lives as a plain integer ON THE VIDEO ROW, maintained
--  by a trigger. `videos` is already world-readable, and a bare count says
--  nothing about who marked what or when.


-- ---------------------------------------------------------------------------
-- 1. The counter
-- ---------------------------------------------------------------------------
alter table public.videos
  add column if not exists times_annotated integer not null default 0;


-- ---------------------------------------------------------------------------
-- 2. Keep it in step with the annotations table, automatically
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER so the trigger may update `videos` even though the public
-- role that inserted the annotation has no write access to that table.
create or replace function public.crt_sync_annotation_count()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if tg_op = 'INSERT' then
    update public.videos
       set times_annotated = times_annotated + 1
     where id = new.video_id;

  elsif tg_op = 'DELETE' then
    update public.videos
       set times_annotated = greatest(0, times_annotated - 1)
     where id = old.video_id;

  elsif tg_op = 'UPDATE' and new.video_id is distinct from old.video_id then
    update public.videos
       set times_annotated = greatest(0, times_annotated - 1)
     where id = old.video_id;
    update public.videos
       set times_annotated = times_annotated + 1
     where id = new.video_id;
  end if;

  return null;   -- AFTER trigger: the return value is ignored
end;
$$;

drop trigger if exists crt_sync_annotation_count on public.annotations;
create trigger crt_sync_annotation_count
  after insert or update or delete on public.annotations
  for each row execute function public.crt_sync_annotation_count();


-- ---------------------------------------------------------------------------
-- 3. Backfill from the marks already collected
-- ---------------------------------------------------------------------------
update public.videos v
   set times_annotated = coalesce(c.n, 0)
  from (select video_id, count(*) as n
          from public.annotations
         where video_id is not null
         group by video_id) c
 where v.id = c.video_id
   and v.times_annotated is distinct from coalesce(c.n, 0);

-- Clips nobody has marked yet settle back to zero.
update public.videos v
   set times_annotated = 0
 where v.times_annotated <> 0
   and not exists (select 1 from public.annotations a where a.video_id = v.id);


-- ---------------------------------------------------------------------------
-- 4. "What has this person already marked?"
-- ---------------------------------------------------------------------------
--  A clip must never be shown twice to the same participant. The browser
--  remembers what it has done, but that memory dies with the browser — a new
--  phone, a cleared cache or a private tab would all resurface old clips.
--
--  This returns ONLY the clip ids for one display name: no times, no frames,
--  no roles, nothing about anybody else. That is the least this can leak and
--  still keep the promise across devices. Participants sign in with initials
--  and no password, so treat it as exactly what it is — a de-duplication
--  helper, never an access-control boundary.
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
-- 5. Check it worked
-- ---------------------------------------------------------------------------
-- Run this on its own afterwards; every clip should show its true mark count,
-- and the spread between the busiest and quietest clip is what the app is
-- there to flatten.
--
--   select video_number, title, times_annotated
--     from public.videos
--    order by times_annotated asc, video_number asc;
