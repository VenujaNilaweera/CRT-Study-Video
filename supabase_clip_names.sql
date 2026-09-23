-- ===========================================================================
--  CRT Study - give every clip a name that means one clip
--  Run this in the Supabase dashboard:  SQL Editor -> New query -> Run.
--  Safe to run more than once.
-- ===========================================================================
--
--  Why this exists
--  ---------------
--  Clip numbers restart at 1 inside every collection. The title was built from
--  that number alone, so the same name existed several times over:
--
--      col1 #7  ->  "CRT Test 07"
--      col2 #7  ->  "CRT Test 07"     <- the same name, a different clip
--      col3 #7  ->  "CRT Test 07"
--
--  A participant reporting "CRT Test 07 will not play" could have meant any of
--  them, and there was no way to tell which from the name. (The in-app Flag
--  button always sent the collection and the clip's uuid as well, so those
--  reports were never ambiguous - but a name said out loud or typed into an
--  email was.)
--
--  This renames every clip so the collection is part of the name:
--
--      col1 #7  ->  "CRT 1-07"
--      col2 #7  ->  "CRT 2-07"
--
--  studio.py builds the same string for new uploads (see site_name()), and
--  index.html falls back to it for any row with no title, so all three agree.
--
--  Nothing but the title changes. video_number, collection_id and above all
--  videos.id are untouched, so every mark already recorded stays attached to
--  exactly the clip it was made on.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. Look before you leap
-- ---------------------------------------------------------------------------
-- Run this on its own first to see which names are currently ambiguous. Every
-- row it returns is a title shared by more than one clip.
--
--   select title, count(*) AS clips
--     from public.videos
--    group by title having count(*) > 1
--    order by clips desc;


-- ---------------------------------------------------------------------------
-- 2. The rename
-- ---------------------------------------------------------------------------
-- 'col2' -> '2'. If a collection_id has no digits in it at all, the id itself
-- is used, so the name is still unique rather than silently collapsing.
UPDATE public.videos v
   SET title = 'CRT '
             || COALESCE(NULLIF(regexp_replace(v.collection_id, '\D', '', 'g'), ''),
                         v.collection_id)
             || '-'
             || lpad(v.video_number::text, 2, '0')
 WHERE v.video_number IS NOT NULL
   AND v.collection_id IS NOT NULL
   -- Idempotent: rows already carrying the new name are skipped, so running
   -- this again is a no-op rather than churning every row.
   AND v.title IS DISTINCT FROM (
         'CRT '
         || COALESCE(NULLIF(regexp_replace(v.collection_id, '\D', '', 'g'), ''),
                     v.collection_id)
         || '-'
         || lpad(v.video_number::text, 2, '0'));


-- ---------------------------------------------------------------------------
-- 3. Check it took
-- ---------------------------------------------------------------------------
-- Should return no rows at all - no title shared by two clips any more:
--
--   select title, count(*) AS clips
--     from public.videos
--    group by title having count(*) > 1;
--
-- And to read the library back in order:
--
--   select collection_id, video_number, title, storage_path
--     from public.videos order by collection_id, video_number;
