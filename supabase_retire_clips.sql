-- ===========================================================================
--  CRT Study - taking a clip down without losing its marks
--  Run this in the Supabase dashboard:  SQL Editor -> New query -> Run.
--  Safe to run more than once.
-- ===========================================================================
--
--  Why this exists
--  ---------------
--  Sooner or later a clip turns out to be wrong: it will not play, the release
--  flash landed on the wrong frame, or the wrong recording was uploaded. That
--  clip has to come off the site.
--
--  The obvious move - DELETE the row - is the one thing you must never do.
--  supabase_hardening.sql ties annotations to videos like this:
--
--      FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
--
--  so deleting one video row silently deletes EVERY mark anyone ever recorded
--  against it. On a study whose whole output is those marks, that is an
--  unrecoverable loss, and nothing warns you it happened.
--
--  So instead of deleting, a clip is RETIRED: active = false. The row stays,
--  its marks stay attached to it and remain analysable, and the site simply
--  stops serving it. Nothing about the numbering changes either - annotations
--  reference videos.id (a uuid), never video_number, so retiring a clip can
--  never cause an existing mark to be counted against a different clip.
--
--  A corrected re-upload becomes a NEW row (new uuid, new storage file) that
--  takes over the retired clip's video_number. Marks made on the bad version
--  stay with the bad version, where they belong, rather than being silently
--  merged into the good one's results.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. The flag itself
-- ---------------------------------------------------------------------------
-- Defaults to true, so every clip already uploaded stays live the moment this
-- runs. Not null, so "unknown" is never a state a clip can be in.
ALTER TABLE public.videos
  ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;

-- Why a clip was taken down, in plain words, for whoever reads the table in a
-- year. Optional - the site never reads it.
ALTER TABLE public.videos
  ADD COLUMN IF NOT EXISTS retired_reason text;

ALTER TABLE public.videos
  ADD COLUMN IF NOT EXISTS retired_at timestamptz;

-- The serving queue asks for the live clips on every sign-in, so give that
-- filter an index rather than a scan.
CREATE INDEX IF NOT EXISTS videos_active_idx
  ON public.videos (active, collection_id, video_number);


-- ---------------------------------------------------------------------------
-- 2. Keep retired_at honest
-- ---------------------------------------------------------------------------
-- Set on the way out, cleared on the way back in, so the timestamp always
-- matches the flag no matter which tool did the update.
CREATE OR REPLACE FUNCTION public.crt_touch_retired_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF new.active = false AND (old.active IS DISTINCT FROM false) THEN
    new.retired_at := now();
  ELSIF new.active = true AND (old.active IS DISTINCT FROM true) THEN
    new.retired_at := NULL;
    new.retired_reason := NULL;
  END IF;
  RETURN new;
END;
$$;

DROP TRIGGER IF EXISTS crt_videos_retired_at ON public.videos;
CREATE TRIGGER crt_videos_retired_at
  BEFORE UPDATE ON public.videos
  FOR EACH ROW EXECUTE FUNCTION public.crt_touch_retired_at();


-- ---------------------------------------------------------------------------
-- 3. How many marks each clip carries
-- ---------------------------------------------------------------------------
-- Studio shows this before it lets you touch a clip: a clip with marks on it
-- may be retired, but must never be deleted. The public key cannot read the
-- annotations table (by design - participants must not see each other's
-- marks), so this is exposed as a view the SERVICE key reads, not the site.
CREATE OR REPLACE VIEW public.crt_clip_usage AS
  SELECT v.id,
         v.collection_id,
         v.video_number,
         v.title,
         v.storage_path,
         v.active,
         v.retired_reason,
         v.retired_at,
         COALESCE(c.n, 0)::bigint AS marks
    FROM public.videos v
    LEFT JOIN (SELECT video_id, count(*) AS n
                 FROM public.annotations
                WHERE video_id IS NOT NULL
                GROUP BY video_id) c
      ON c.video_id = v.id;

-- Readable only by the service key. anon/authenticated are deliberately NOT
-- granted: the view carries mark counts, which the study site has no business
-- reading.
REVOKE ALL ON public.crt_clip_usage FROM anon, authenticated;


-- ---------------------------------------------------------------------------
-- 4. What the site sees
-- ---------------------------------------------------------------------------
-- index.html filters retired clips out in JS (row.active !== false) rather
-- than asking PostgREST for active=is.true. That is deliberate: it means the
-- site behaves correctly whether or not this file has been run yet, instead of
-- every clip request failing with a 400 on an unknown column until it is.
--
-- Nothing below is required. It is here only if you would rather the filter
-- were enforced by the database as well:
--
--   -- ALTER TABLE public.videos ENABLE ROW LEVEL SECURITY;
--   -- DROP POLICY IF EXISTS videos_public_read ON public.videos;
--   -- CREATE POLICY videos_public_read ON public.videos
--   --   FOR SELECT TO anon USING (active);


-- ---------------------------------------------------------------------------
-- 5. Handy checks
-- ---------------------------------------------------------------------------
-- Everything currently taken down, and why:
--   select video_number, title, retired_reason, retired_at
--     from public.videos where not active order by retired_at desc;
--
-- Clips carrying marks (never delete one of these):
--   select video_number, title, active, marks
--     from public.crt_clip_usage where marks > 0 order by marks desc;
