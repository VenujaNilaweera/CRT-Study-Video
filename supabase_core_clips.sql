-- ===========================================================================
--  CRT Study - the "first 10": the same hand-picked clips, first, for everyone
--  Run this in the Supabase dashboard:  SQL Editor -> New query -> Run.
--  Safe to run more than once.
-- ===========================================================================
--
--  Why this exists
--  ---------------
--  Every participant is normally handed clips least-annotated first, shuffled
--  (supabase_balancing.sql), so coverage evens out across the library. That
--  means no two people see the same clips - fine for coverage, but it gives no
--  common ground to compare observers on.
--
--  The fix is a small core set: up to 10 clips, picked by hand in Studio
--  ("On the site" tab -> "Add to first 10"), that EVERY participant marks
--  first, in the same order. Only after those does the usual balancing take
--  over for everything else.
--
--  videos.core_order holds the position (1 = shown first). NULL = an ordinary
--  clip. Nothing else changes: a core clip is still counted in
--  times_annotated, and its marks are ordinary marks.
--
--  The site reads the column with the rest of the videos row, so it works the
--  moment this is run - and if it has not been run, the site simply behaves as
--  before (no core set).
-- ===========================================================================

ALTER TABLE public.videos ADD COLUMN IF NOT EXISTS core_order smallint;

ALTER TABLE public.videos DROP CONSTRAINT IF EXISTS videos_core_order_positive;
ALTER TABLE public.videos ADD  CONSTRAINT videos_core_order_positive
  CHECK (core_order IS NULL OR core_order >= 1);

-- One clip per position, so the order can never be ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS videos_core_order_key
  ON public.videos (core_order) WHERE core_order IS NOT NULL;

-- Studio reads clips through this view (supabase_retire_clips.sql); add the
-- new column at the end so Studio can show and change the first 10.
CREATE OR REPLACE VIEW public.crt_clip_usage AS
  SELECT v.id,
         v.collection_id,
         v.video_number,
         v.title,
         v.storage_path,
         v.active,
         v.retired_reason,
         v.retired_at,
         COALESCE(c.n, 0)::bigint AS marks,
         v.core_order
    FROM public.videos v
    LEFT JOIN (SELECT video_id, count(*) AS n
                 FROM public.annotations
                WHERE video_id IS NOT NULL
                GROUP BY video_id) c
      ON c.video_id = v.id;

REVOKE ALL ON public.crt_clip_usage FROM anon, authenticated;


-- ---------------------------------------------------------------------------
-- Handy checks
-- ---------------------------------------------------------------------------
-- The current first 10, in the order participants get them:
--   select core_order, title, storage_path, active
--     from public.videos where core_order is not null order by core_order;
--
-- How many marks each core clip has (should climb together):
--   select core_order, title, marks from public.crt_clip_usage
--    where core_order is not null order by core_order;
