# CRT Perception Study

A web application for collecting **capillary refill time (CRT) measurements** from medical professionals watching video clips. Each participant signs in, watches a clip of a capillary refill test (the colour-return moment after releasing finger pressure on skin), and presses one button at the exact instant they perceive the colour returning. The app records the precise **time and video frame** of that observation and saves it straight to the study's Supabase database, pooling judgements from many professionals to establish a consensus refill threshold.

## Project Goal

**Why:** Capillary refill time (CRT) is a key clinical assessment, but perception of when colour "returns" varies significantly between observers. Medical professionals disagree on what constitutes colour return, making CRT measurements inconsistent.

**What we're doing:** This study collects real-time observations from nurses, doctors, paramedics, and medical students as they watch standardized CRT video clips. By analyzing when different professionals mark the colour return, we can:
- Quantify the variability in CRT perception
- Identify factors that influence observer agreement
- Establish objective thresholds for colour-return detection
- Improve clinical training and standardization

**Output:** A dataset of time-stamped observations (with frame-level precision) from diverse medical professionals, enabling statistical analysis of perception variance.

---

## How the site runs today

This is a **static site with a Supabase backend** - there is no local server to run for participants to use it.

- `index.html` + `styles.css` are the entire app: login, the onboarding walkthrough, the clip player, and the About/feedback page.
- The live site is **GitHub Pages**, serving the `gui` branch directly (Settings → Pages, no build step). Push to `gui` and it's live within a minute or two.
- All data - clips, collections, and every submitted annotation - lives in **Supabase**:
  - `collections` and `videos` tables describe the clip library (the `videos` table also carries each clip's timing metadata, see [Stamp / timing metadata](#stamp--timing-metadata-per-clip)).
  - The `vids` Storage bucket holds the actual `.mp4` files, served publicly.
  - The `annotations` table receives one row per submitted mark (see [Data output](#data-output)).
  - A couple of RPC functions (`crt_seen_videos`, `crt_lookup_participant`) back the least-annotated-first queue and the "welcome back" name lookup - both optional, see [SQL migrations](#sql-migrations-run-once-per-supabase-project).
- The app talks to Supabase with its **public anon key** (safe to expose - it's baked into `index.html`), which can only insert annotations, never read them back or edit videos/collections. Everything else is locked down by Row Level Security plus `supabase_hardening.sql`.

Nothing here needs `excel_helper.py`, a local `videos/` folder, or `data.xlsx` any more - those belonged to an earlier, local-only version of this app. They're described at the bottom under [Legacy scripts](#legacy-scripts) for anyone who still has a use for them, but the live site doesn't call any of them.

---

## Running it locally

To work on the UI itself, you don't need your own Supabase project - `index.html` already points at the study's real one.

```bash
python -m http.server 8899
```

Then open <http://127.0.0.1:8899/index.html>. That's it - clips, collections and the onboarding video load straight from Supabase Storage, exactly as they do on the live site.

The app persists login and video position to `localStorage` and restores them on reload, so an accidental refresh mid-clip doesn't lose your place.

### Remote access (phone/tablet on the same WiFi)

Instead of `127.0.0.1`, use your PC's LAN IP (e.g. `192.168.1.100`) so another device on the same network can open the same server.

---

## Setting up your own Supabase project

Only needed if you're standing up a **separate** instance of this study (a different Supabase project from the one already wired into `index.html`).

1. **Create the tables.** You'll need `collections`, `videos`, and `annotations`. The shape each needs is documented at the top of the three SQL files below (they describe the columns they depend on) and in the `videos` payload shape under [Stamp / timing metadata](#stamp--timing-metadata-per-clip) and the `annotations` row shape under [Data output](#data-output).
2. **Create a public Storage bucket** named `vids` for the `.mp4` files.
3. **Point `index.html` at your project** - update `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `STORAGE_BUCKET` near the top of the `<script>` block (search for `SUPABASE BACKEND`).
4. **Run the SQL migrations** (below) in the Supabase dashboard's SQL Editor.
5. **Upload your clips** with `studio.py` (below).

### SQL migrations (run once per Supabase project)

Each file is self-contained, explains itself at the top, and is safe to run more than once. Open the Supabase dashboard → SQL Editor → New query → paste the file → Run.

| File | What it's for | If you skip it |
|---|---|---|
| `supabase_hardening.sql` | Locks down the `annotations` table so the public key can only insert well-formed rows - nothing malformed, nothing read back. | Recommended for any project taking public submissions. |
| `supabase_balancing.sql` | Lets the app hand each participant the **least-annotated** clips first, via the `crt_seen_videos` RPC, without giving the public key read access to `annotations`. | The app falls back to a plain random order - it still works, just without active balancing. |
| `supabase_returning_users.sql` | Powers the "welcome back" prompt: recognising a name that's signed in before and prefilling their role/age. Adds the `crt_lookup_participant` RPC. | The name field behaves like a normal first-time field - no prefill, no error either. |
| `supabase_retire_clips.sql` | Lets a bad clip be taken off the site without losing its marks. Adds `videos.active` plus the `crt_clip_usage` view Studio reads mark counts from. | Studio's "Published clips" window can still list clips, but cannot take any down and cannot show mark counts. |

The app is written to degrade gracefully if any of these haven't been run yet - it just quietly does the simpler thing instead of erroring.

---

## Adding video clips

Clips are added with **`studio.py`**, a desktop tool (`python studio.py`) that takes a folder of raw recordings all the way to the live study site in one pass: add the release-flash overlay → re-encode for the browser → upload to Supabase Storage → create the database rows → group the clips into collections. Read the docstring at the top of the file for the full pipeline and what it needs (ffmpeg/ffprobe on PATH, `pip install opencv-python requests`, and a Supabase **service role** key pasted into Settings once - never the public key, and never committed; it's saved to `studio_config.json`, which is git-ignored).

Reload the site afterwards and the new clips just appear - nothing in `index.html` needs editing.

### Taking a bad clip down, and replacing it

When someone reports that a clip will not play, or the release flash landed on the wrong frame, open **Published clips...** in Studio. It lists every clip on the site with both names side by side - what participants call it (`CRT Test 07`) and which recording it came from - so a report naming "CRT Test 7" leads straight to the file. Type `7`, or part of the filename, in the **Find** box.

- **Take down** retires the clip: it stops being served immediately, and every mark already recorded against it is kept and stays analysable. Reversible with **Put back**.
- **Replace file...** takes the corrected recording, retires the version that is live now, and uploads the new one under the *same clip number*, so the study's numbering does not shift under anyone.
- **Delete permanently** is offered only for a clip that nothing has marked yet.

**Never delete a clip row by hand in the Supabase dashboard.** `supabase_hardening.sql` declares

```sql
FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
```

so deleting one video row silently deletes every mark anyone ever recorded against it, with no warning and no way back. Retiring is there precisely so you never have to.

Two details worth knowing, both deliberate:

- A replacement is a **new row with a new storage filename**, not an edit of the old one. Uploads use `x-upsert`, so re-using the filename would overwrite the original file - and the retired row would then point at the new footage, quietly attaching the bad clip's marks to the good clip's video. Keeping them separate means marks made on the broken version stay with the broken version, where they belong, instead of being merged into the corrected one's results.
- Retiring never disturbs anything already recorded. Annotations reference `videos.id` (a uuid), never `video_number`, so a mark can never be re-attributed to a different clip by anything that happens to the numbering.

### Frame rate overrides

Frame rate for the mark → frame-number conversion comes from each clip's `videos` row; if you need a manual override for a specific file, add it to `FPS_OVERRIDES` near the top of `index.html`, keyed by the clip's storage path:
```javascript
const FPS_OVERRIDES = {
  'slow_motion_clip.mp4': 60,
};
```

---

## Stamp / timing metadata (per clip)

Each recording **can** carry metadata for the moment the finger pressure was released - critical for calculating real capillary refill time (CRT), as opposed to just "time since the clip started."

**Why it matters:**
- Raw video time starts at 0 (beginning of the recording)
- But the actual test begins *after* the camera starts, when the tester applies pressure
- The release moment marks when CRT measurement officially starts
- Without it, the app treats the start of the clip as time 0
- With it, the app calculates **real CRT = (markedFrame − stampFrame) / realFps**, the true refill time since release

For a raw recording (before it goes through `studio.py`), that's a `<filename>.stamp.json` sidecar file next to the `.mp4`:

```json
{
  "video": "2026-08-16_13-31-57-901.mp4",
  "trigger_source": "PC",
  "frame_count": 152,
  "recording_duration_s": 9.109,
  "fps": 16.69,
  "stamped": true,
  "stamped_frame": 67,
  "stamp_time_s": 4.078,
  "post_stamp_tail_s": 5.0
}
```

| Field | Type | Description |
|-------|------|--------------|
| `video` | string | Filename of the `.mp4` it describes (informational) |
| `trigger_source` | string | Where the timestamp came from (e.g. "PC", "RPi", "manual") |
| `frame_count` | integer | Total frames in the recording (used for precise fps calculation) |
| `recording_duration_s` | float | Duration of the recording in seconds (`frame_count / recording_duration_s` = real fps) |
| `fps` | float | Frame rate fallback; overridden by `frame_count / recording_duration_s` when both are present |
| `stamped` | boolean | Whether a pressure-release timestamp exists for this clip |
| `stamped_frame` | integer | **Frame number where pressure was released** - the reference point for CRT measurement |
| `stamp_time_s` | float | Time in seconds when pressure was released (video-file time) |
| `post_stamp_tail_s` | float | Duration after release (how long the colour-return portion was recorded) |

`studio.py` reads this sidecar file and carries the same fields into the `videos` table row it creates (`encoded_fps`, `frame_count`, `recording_duration_s`, `stamped`, `stamped_frame` - see the app's `SUPABASE BACKEND` comment block in `index.html` for the exact column names it reads). Once a clip is live, the player uses those columns the same way regardless of how the clip got there:

1. **Video playback:** jumps to the release frame on load, for a stamped clip
2. **Frame calculation:** marks are converted to frame numbers using the clip's encoded fps
3. **CRT calculation:** real refill time = `(markedFrame − stampFrame) / realFps`
4. **Display:** the scrub bar shows a tick at the release point; the readout shows time *since release* (never negative)

**Clips without a stamp** use the raw video timeline (time 0 = start of clip); the "Time from pressure release" label changes to just "Video time".

---

## The onboarding walkthrough

The first time someone signs in with a new name, they see a two-phase intro before reaching the player:

1. **Three illustrated cards** - a short "how this works" explanation (Next/Back, with progress dots), ending on "Watch the demo".
2. **A recorded demo video** that auto-plays and pauses itself at six captioned moments, walking through an actual clip end-to-end. "Next" resumes it; the last step drops straight into the player.

Completing it (or skipping it) is recorded per **participant name**, not per browser or device - several people can share one phone in a ward without each having to sit through it, but the same name never sees it twice.

The **"How it works"** link in the side menu replays just the recorded video (no cards - those are a first-run welcome only), for anyone who wants a refresher later.

On a phone, the video forces itself into full-screen landscape regardless of which way the phone is actually held (with a brief "turn your phone sideways" card first), so the demo is always readable.

---

## Data output

Every submitted mark is inserted as one row into Supabase's **`annotations`** table.

| Column | Description |
|---|---|
| `display_name` | Participant's name, as typed at sign-in |
| `role` | Profession (Nurse, Doctor, Medical student, Paramedic, Researcher, Other) |
| `age_group` | Age bracket (18-24, 25-34, 35-44, 45-54, 55+) |
| `video_id` | The clip's id in the `videos` table |
| `skipped` | Whether the clip was skipped instead of marked |
| `mark_file_time_s` | Position in the video file where colour return was marked (seconds) |
| `frame_number` | Frame number at the mark, `Math.floor(markTime × fps)` |
| `crt_s` | **Study value:** real refill time since release, `(frame_number − stamp_frame_used) / fps_used` for stamped clips; raw mark time for unstamped ones |
| `fps_used` | Capture frame rate driving the CRT calculation |
| `release_file_time_s` | Position in the video file where pressure was released; empty if unstamped |
| `stamp_frame_used` | Recording frame of the pressure release; empty if unstamped |
| `submitted_at` | ISO 8601 timestamp of the submission (UTC) |

### Understanding the data

**For stamped clips:** `crt_s` is the true refill time since pressure release - this is the study value. `frame_number` and `stamp_frame_used` let you trace back to the exact frames in the original recording.

**For unstamped clips:** `crt_s` equals `mark_file_time_s` - it's just raw video time, with no `stamp_frame_used` or `release_file_time_s`. Useful for sanity-checking timings and sequences.

---

## Features

### Serving order
- **Least-annotated-first:** clips are queued so every recording in the library collects a comparable number of judgements, not just the first few taking them all (needs `supabase_balancing.sql`)
- **Sets of 20:** clips come in runs of 20 with a natural stopping point; "Continue" starts the next set with fresh clips
- **Never repeats:** a participant is never shown a clip they've already marked

### UI/UX
- **Dark mode & light mode:** toggle from the side menu; preference is saved
- **Responsive design:** built for phone, tablet, and desktop
- **Keyboard shortcuts (player):** Space or K (play/pause), ← → or , . (frame step; Shift = 10 frames), ↓ ↑ or J L (seek 1s), M (mark), N (save & next), R (restart)
- **Keyboard shortcuts (onboarding):** → (next), ← (back), Esc (skip)
- **"Welcome back":** offers to continue as the last name/role/age used on this device, and can recognise a returning name from a different device too (needs `supabase_returning_users.sql`)

### Robustness
- **Session recovery:** login and video position survive an accidental page reload
- **Sync queue:** if Supabase is briefly unreachable, marks stay queued locally; a "N unsaved" button lets the participant retry
- **Full-clip preload:** the next clip loads ahead of time so pressing "Next" doesn't stall on a slow connection
- **"Flag a problem":** a one-click report for a clip that won't play, distinct from Skip - tells the research team exactly which clip failed, no typing required

### Data integrity
- **Frame-accurate timing:** uses `Math.floor(time × fps)`, never rounds, so frame numbers are never off by half a frame
- **Precise CRT calculation:** bridges encoding frame rate (the `.mp4` file's own fps) and capture frame rate (from the stamp metadata) using frame numbers as the stable reference

### Getting in touch
- **About page:** background on the study, the research team, and the ethics/privacy statement
- **Feedback form:** goes straight to the research team by email (via FormSubmit, with a `mailto:` fallback if that ever fails)

---

## Tips & Troubleshooting

### Videos won't play
- They need to be H.264 (Baseline) with AAC audio - `studio.py` re-encodes for this during upload
- Check the browser console (F12) for a specific codec error

### Marks not saving
- Check the browser console for a failed request to Supabase
- The "N unsaved" button appears if marks are queued locally - click it to retry
- Confirm `SUPABASE_URL` / `SUPABASE_ANON_KEY` in `index.html` still match your project, and that `supabase_hardening.sql` hasn't been run in a way that blocks inserts

### A new participant doesn't get "least-annotated-first" order, or a returning name isn't recognised
- Those need `supabase_balancing.sql` / `supabase_returning_users.sql` to have been run - see [SQL migrations](#sql-migrations-run-once-per-supabase-project). The app itself won't error either way, it just falls back to the simpler behaviour.

### Frame numbers seem off
- Check the clip's `frame_count` and `recording_duration_s` in the `videos` table match the actual recording
- Verify the clip's encoded fps, or add an entry to `FPS_OVERRIDES`

---

## Project structure

```
CRT-Study-Video/
├── index.html                    # The entire web app (single page)
├── styles.css                    # Theme tokens and layout
├── studio.py                     # Desktop tool: raw recordings -> live site (flash, encode, upload, DB rows)
├── supabase_hardening.sql        # SQL migration - lock down annotation inserts
├── supabase_balancing.sql        # SQL migration - least-annotated-first serving
├── supabase_returning_users.sql  # SQL migration - "welcome back" name lookup
├── supabase_retire_clips.sql     # SQL migration - take a clip down without losing its marks
├── make_walkthrough_images.py    # Build script for the onboarding cards' illustrations
├── assets/                       # Images + the onboarding demo video, served by the site
├── walkthrough_src/              # Source crops for make_walkthrough_images.py
├── requirements.txt              # Python dependencies for the scripts above
└── README.md                     # This file
```

### Legacy scripts

`excel_helper.py` and `convert_videos.py` belong to an earlier, local-only version of this app (annotate into `data.xlsx`, drop clips into a local `videos/` folder). The live site doesn't call either any more - `studio.py` + Supabase replaced that whole workflow. They're kept here for reference only.

---

## Git Branch & GitHub Account Organization

⚠️ **This repository can have two remotes - make sure you're pushing to the correct one.**

### Your account (origin) - primary development
- **GitHub:** https://github.com/VenujaNilaweera/CRT-Study-Video
- **Branch:** `gui` - what GitHub Pages actually serves; pushing here goes live
- Use this for your own development and GUI improvements

### Pansilu's account (upstream) - reference/original, if you've added it
- **GitHub:** https://github.com/PansiluHarshan/CRT-Study-Video
- **Branch:** `main` - do not push here
- Pull-only, to stay synced with the original if needed

### Key commands

**Check which remote you're using:**
```bash
git remote -v
```

**Push ONLY to your account:**
```bash
git push origin gui
```

**Pull Pansilu's changes (read-only), if `upstream` is configured:**
```bash
git fetch upstream
git diff upstream/main   # review before merging
```

**Avoid accidentally pushing to upstream:**
```bash
git remote set-url --push upstream DISABLE
```
