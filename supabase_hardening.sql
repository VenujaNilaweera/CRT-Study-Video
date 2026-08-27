-- ===========================================================================
--  CRT Study - database hardening
--  Run this in the Supabase dashboard:  SQL Editor -> New query -> Run.
--  Safe to run more than once.
-- ===========================================================================
--
--  Why this exists
--  ---------------
--  Participants submit annotations with the site's PUBLIC key, so that one
--  path has to stay open — it is the study. Everything else (editing videos,
--  editing collections, uploading files, reading other people's marks) is
--  already blocked by row-level security; this file closes the remaining gap
--  by making it impossible to store a nonsensical annotation, even from a
--  hand-crafted request that never touched the website.


-- ---------------------------------------------------------------------------
-- 1. Stop the same clip being published twice
-- ---------------------------------------------------------------------------
-- studio.py already skips clips in its ledger and in the database; this makes
-- a duplicate impossible at the storage layer as well.
ALTER TABLE videos DROP CONSTRAINT IF EXISTS videos_storage_path_key;
ALTER TABLE videos ADD  CONSTRAINT videos_storage_path_key UNIQUE (storage_path);


-- ---------------------------------------------------------------------------
-- 2. Reject impossible annotation values
-- ---------------------------------------------------------------------------
-- A real capillary refill measurement cannot be negative, and anything past
-- a minute is not a refill time — it is noise or a deliberately bad payload.
ALTER TABLE annotations DROP CONSTRAINT IF EXISTS annotations_crt_sane;
ALTER TABLE annotations ADD  CONSTRAINT annotations_crt_sane
  CHECK (crt_s IS NULL OR (crt_s >= 0 AND crt_s <= 60));

ALTER TABLE annotations DROP CONSTRAINT IF EXISTS annotations_frame_sane;
ALTER TABLE annotations ADD  CONSTRAINT annotations_frame_sane
  CHECK (frame_number IS NULL OR (frame_number >= 0 AND frame_number <= 100000));

ALTER TABLE annotations DROP CONSTRAINT IF EXISTS annotations_fps_sane;
ALTER TABLE annotations ADD  CONSTRAINT annotations_fps_sane
  CHECK (fps_used IS NULL OR (fps_used > 0 AND fps_used <= 1000));

-- Cap the free-text fields so nobody can inflate the table with huge strings.
ALTER TABLE annotations DROP CONSTRAINT IF EXISTS annotations_name_len;
ALTER TABLE annotations ADD  CONSTRAINT annotations_name_len
  CHECK (display_name IS NULL OR char_length(display_name) <= 60);

ALTER TABLE annotations DROP CONSTRAINT IF EXISTS annotations_role_len;
ALTER TABLE annotations ADD  CONSTRAINT annotations_role_len
  CHECK (role IS NULL OR char_length(role) <= 40);

-- Every annotation must point at a real clip, and must disappear with it.
ALTER TABLE annotations DROP CONSTRAINT IF EXISTS annotations_video_fk;
ALTER TABLE annotations ADD  CONSTRAINT annotations_video_fk
  FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE;


-- ---------------------------------------------------------------------------
-- 3. Server-side timestamp
-- ---------------------------------------------------------------------------
-- submitted_at arrives from the browser, so it can be forged. Keep a column
-- the client cannot influence, and trust this one when analysing.
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS server_received_at timestamptz DEFAULT now();


-- ---------------------------------------------------------------------------
-- 4. Confirm the public key is still write-only on annotations
-- ---------------------------------------------------------------------------
-- Participants must be able to INSERT their own marks but never read, edit or
-- delete anyone's. Verified working on 2026-08-27; re-run after any policy
-- change. Expect exactly one INSERT policy for anon.
SELECT tablename, policyname, cmd, roles
FROM   pg_policies
WHERE  schemaname = 'public'
ORDER  BY tablename, cmd;


-- ---------------------------------------------------------------------------
-- 5. Useful views for keeping an eye on incoming data
-- ---------------------------------------------------------------------------
-- Submissions per day per person: a sudden burst from one name is the signal
-- that someone is scripting the endpoint rather than watching clips.
CREATE OR REPLACE VIEW annotation_activity AS
SELECT date_trunc('day', server_received_at) AS day,
       display_name,
       role,
       count(*)                              AS submissions,
       round(avg(crt_s)::numeric, 3)         AS avg_crt_s,
       min(crt_s)                            AS min_crt_s,
       max(crt_s)                            AS max_crt_s
FROM   annotations
GROUP  BY 1, 2, 3
ORDER  BY 1 DESC, submissions DESC;

-- Marks that look automated: many submissions in under a minute.
CREATE OR REPLACE VIEW annotation_bursts AS
SELECT display_name,
       date_trunc('minute', server_received_at) AS minute,
       count(*)                                 AS submissions
FROM   annotations
GROUP  BY 1, 2
HAVING count(*) > 10
ORDER  BY minute DESC;
