# CRT Perception Study

A web application for collecting **capillary refill timing (CRT) measurements** from medical professionals watching video clips. Each participant logs in, watches a clip of a capillary refill test (the colour-return moment after releasing finger pressure on skin), and presses one button at the exact instant they perceive the colour returning. The app records the precise **time and video frame** of that observation and saves it to `data.xlsx`, pooling judgements from many professionals to establish a consensus refill threshold.

## Project Goal

**Why:** Capillary refill time (CRT) is a key clinical assessment, but perception of when colour "returns" varies significantly between observers. Medical professionals disagree on what constitutes colour return, making CRT measurements inconsistent.

**What we're doing:** This study collects real-time observations from nurses, doctors, paramedics, and medical students as they watch standardized CRT video clips. By analyzing when different professionals mark the colour return, we can:
- Quantify the variability in CRT perception
- Identify factors that influence observer agreement
- Establish objective thresholds for colour-return detection
- Improve clinical training and standardization

**Output:** A dataset in `data.xlsx` containing time-stamped observations (with frame-level precision) from diverse medical professionals, enabling statistical analysis of perception variance.

---

## Running it

### Quick Start

1. **Start the Excel saver** (writes each click into `data.xlsx` on port 8787):
   ```bash
   python excel_helper.py
   ```
   This starts a small HTTP server that accepts annotation submissions and appends them to the workbook.

2. **Serve the site** (makes videos discoverable and listable on port 8899):
   ```bash
   python -m http.server 8899
   ```

3. Open <http://127.0.0.1:8899/index.html> in a browser.

### Important: Don't use live-reload servers

**Do not** use VS Code "Live Server", `live-server`, or other file-watching servers. Here's why:
- Every time a mark is saved, `excel_helper.py` writes `data.xlsx` inside this folder
- Live-reload servers detect that file change and refresh the page
- This restarts the current video clip, which is annoying for the participant
- Plain `python -m http.server` doesn't watch files, so it won't trigger unwanted refreshes

The app now persists login and video position across reloads, so even accidental refreshes don't lose your place.

### Remote access (phone/tablet on same WiFi)

When you run the servers on your PC, other devices on the same network can access them:
- Instead of `127.0.0.1`, replace it with your PC's LAN IP (e.g., `192.168.1.100`)
- `excel_helper.py` automatically detects the correct IP and logs it on startup
- The web app will save to your PC's workbook, not the device's local storage

---

## Adding and Preparing Videos

### Dropping clips in

1. Place your `.mp4` clips in the **`videos/`** folder (next to `index.html`)
2. The app auto-discovers everything there — no code editing needed
3. Reload the browser page and new clips appear

### Browser compatibility & encoding

⚠️ **A file ending in `.mp4` is *not* guaranteed to play in a browser.** Screen recorders often produce MPEG-4 Part 2, DivX, or HEVC codecs that browsers refuse to play.

**To ensure all clips play:**
```bash
python convert_videos.py
```
This re-encodes anything that isn't already **H.264 (Baseline)** to a browser-safe format, while preserving:
- Frame rate (fps)
- Frame count (so frame numbers remain valid)
- Duration and timing (timestamps don't shift)

Originals are backed up in `videos/_original_backup/`.

### Multiple collections (folders)

Want to organize videos into separate collections (e.g., "Beginner clips" vs. "Advanced clips")?

Edit `VIDEO_FOLDERS` near the top of `index.html`:
```javascript
const VIDEO_FOLDERS = [
  { id: 'col1', name: 'Collection 01', icon: '📁', path: 'videos/' },
  { id: 'col2', name: 'Collection 02', icon: '📁', path: 'videos2/' },
  // Add more as needed
];
```
Each collection shows up as a separate tile on the collections screen.

### Frame rate overrides

Frame rate defaults to **30 fps** and is used to convert marked times into frame numbers. If individual clips run at a different rate, add overrides to `FPS_OVERRIDES` in `index.html`:
```javascript
const FPS_OVERRIDES = {
  'slow_motion_clip.mp4': 60,
  'low_fps_clip.mp4': 24,
};
```

---

## JSON Stamp Files: Timestamping the Pressure Release

### What are stamp files?

Each video clip **can** have an optional `<filename>.stamp.json` file that records **when the finger pressure was released** during the test. This is critical for calculating real capillary refill time (CRT).

**Why it matters:**
- Raw video time starts at 0 (beginning of recording)
- But the actual test begins *after* the camera starts, when the tester applies pressure
- The "stamp" marks the moment pressure is released — that's when CRT measurement officially starts
- Without a stamp, the app treats the start of the clip as time 0
- With a stamp, the app calculates **real CRT = (markedFrame − stampFrame) / realFps**, giving the true refill time since release

### Stamp file format

A stamp file is JSON with this structure:

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

#### Field reference

| Field | Type | Description |
|-------|------|-------------|
| `video` | string | Filename of the `.mp4` it describes (informational) |
| `trigger_source` | string | Where the timestamp came from (e.g., "PC", "RPi", "manual") |
| `frame_count` | integer | Total frames in the video recording (used for precise fps calculation) |
| `recording_duration_s` | float | Duration of the recording in seconds (used to calculate exact fps: `frame_count / recording_duration_s`) |
| `fps` | float | Frame rate (fallback; overridden by `frame_count / recording_duration_s` if both are present) |
| `stamped` | boolean | Whether a pressure-release timestamp exists for this clip (`true` = yes, `false` = no) |
| `stamped_frame` | integer | **Frame number where pressure was released** (the reference point for CRT measurement) |
| `stamp_time_s` | float | Time in seconds when pressure was released (video-file time) |
| `post_stamp_tail_s` | float | Duration after release (how long the colour-return portion was recorded) |

### How it's used in the app

1. **Video playback:** The player automatically jumps to the release frame when a stamped clip loads
2. **Frame calculation:** Marked observations are converted to frame numbers using the encoded file's fps
3. **CRT calculation:** Real refill time = `(markedFrame − stampFrame) / realFps`
4. **Display:** The scrub bar shows a tiny tick at the release point; the readout shows time *since release* (never negative)

### Creating stamp files

If you're generating CRT clips (e.g., with a Raspberry Pi or automated recording setup), create a `.stamp.json` file for each `.mp4`:

**Naming:** The JSON filename must match the video exactly, minus the extension:
- `2026-08-16_13-31-57-901.mp4` → `2026-08-16_13-31-57-901.stamp.json`

**Generating the timestamp:** Capture the frame number where you release pressure in real time (e.g., via a hardware trigger, GPIO pin, or button press), then calculate:
```
realFps = frame_count / recording_duration_s
stamp_time_s = stamped_frame / fps  (file fps, not real fps)
```

**Example workflow (Python):**
```python
import json
from pathlib import Path

stamp = {
    "video": "mytest.mp4",
    "trigger_source": "GPIO_button",
    "frame_count": 300,
    "recording_duration_s": 9.96,  # measured from file metadata
    "fps": 30.0,  # encoded frame rate
    "stamped": True,
    "stamped_frame": 120,  # button press on frame 120
    "stamp_time_s": 4.0,
    "post_stamp_tail_s": 5.96
}

Path("mytest.stamp.json").write_text(json.dumps(stamp, indent=2))
```

### Clips without stamps

If a clip has no `.stamp.json` file, the app:
- Uses the raw video timeline (time 0 = start of clip)
- Detects fps from playback (or uses `FPS_OVERRIDES`)
- Treats marked observations as raw video times, not CRT
- The "Time from pressure release" label changes to just "Video time"

---

## Data Output

### Excel workbook: `data.xlsx`

Every time a participant marks and saves a clip, a new row is appended to `data.xlsx` (sheet **Annotations**).

#### Columns

| Column | Type | Description |
|--------|------|-------------|
| **Name** | text | Participant's full name |
| **Role** | text | Profession (Nurse, Doctor, Medical student, Paramedic, Researcher, Other) |
| **Age Group** | text | Age bracket (18-24, 25-34, 35-44, 45-54, 55+) |
| **Collection** | text | Folder/collection name (e.g., "Collection 01") |
| **Video #** | integer | Sequence in the collection (1-indexed) |
| **Video Title** | text | Auto-generated title (e.g., "CRT Test 01") |
| **CRT (s)** | float (3 decimal places) | **Study value:** Real refill time since release. Calculated as `(markedFrame − stampFrame) / realFps` for stamped clips; raw mark time for unstamped clips |
| **Mark file-time (s)** | float | Position in the video file where colour return was marked (in seconds) |
| **Release file-time (s)** | float | Position in the video file where pressure was released (from `stampFrame / fps`); empty if no stamp |
| **FPS** | float | Capture frame rate that drives CRT calculation (from `.stamp.json` or manual override; 30 fps default) |
| **Frame #** | integer | Recording frame number at the mark (calculated as `round(markTime × fps)`) |
| **Stamp frame** | integer | Recording frame of the pressure release; empty if no stamp |
| **Submitted At** | ISO 8601 timestamp | When the mark was saved (UTC) |

### CSV export

Each participant can click **"Export log"** in the header to download a CSV of all their own marks. Columns match the Excel layout above.

### Understanding the data

**For stamped clips:**
- **CRT (s)** is the true refill time since pressure release — this is your study value
- **Frame #** and **Stamp frame** let you trace back to the exact frames in the original recording
- **Release file-time (s)** tells you where in the encoded `.mp4` the release occurs

**For unstamped clips:**
- **CRT (s)** is the same as **Mark file-time (s)** — it's just raw video time
- No **Stamp frame** or **Release file-time**
- Useful for sanity-checking timings and sequences

---

## Architecture & Storage

### Client-side (browser)

- **Session persistence:** Login, current video, and position are saved to `localStorage` and restored on page reload
- **Annotation queuing:** Marks are saved to local storage immediately, then synced to the backend
- **Offline resilience:** If the Excel helper is unreachable, marks stay queued locally; the "N unsaved" button lets users retry

### Server-side (Python)

- **excel_helper.py (port 8787):** HTTP server that receives annotation submissions and appends them to `data.xlsx`
  - Accepts CORS requests from any origin
  - Handles concurrent requests with thread pooling
  - Auto-creates the workbook and sheet on first run
  - Returns row number and workbook path in JSON response

- **HTTP server (port 8899):** Plain Python file server
  - Lists files in `videos/` and `videos2/` (if configured)
  - Serves `.mp4`, `.stamp.json`, and other assets
  - No special logic — just standard HTTP directory serving

---

## Project Structure

```
CRT-Study-Video/
├── index.html              # Main web app (single-page, ~850 lines)
├── styles.css              # Theme tokens and layout
├── excel_helper.py         # Backend: Excel workbook server (port 8787)
├── convert_videos.py       # Video re-encoding utility
├── data.xlsx               # Output spreadsheet (created on first run)
├── README.md               # This file
├── videos/                 # Video clips go here
│   ├── mytest.mp4
│   ├── mytest.stamp.json   # (optional) Pressure release timestamp
│   └── ...
├── videos/_original_backup/  # Backups from convert_videos.py
└── work/                    # Build/utility scripts (not needed for running the app)
```

---

## Features

### UI/UX
- **Dark mode & light mode:** Toggle with the theme button; preference is saved
- **Responsive design:** Optimized for phone, tablet, and desktop
- **Keyboard shortcuts:** Space (play/pause), ← → (frame step), Shift+← → (10-frame jump), M (mark), N (save & next), R (restart)
- **Playback speed controls:** 0.25×, 0.5×, 1× — useful for slow-motion analysis
- **Frame-by-frame navigation:** Pause and step one frame at a time with arrow keys
- **Scrub bar with release marker:** Visual indicator of where the pressure release frame lies

### Robustness
- **Session recovery:** Login and video position survive accidental page reloads (or live-reload events)
- **Sync queue:** Marks stay queued locally if the backend is offline; retry with the "N unsaved" button
- **Video metadata detection:** Auto-measures frame rate from playback (with timestamp accuracy) for clips without overrides
- **Graceful fallbacks:** If folder listing fails (e.g., `file://` opens), you can manually list videos in `MANUAL_COLLECTIONS`

### Data integrity
- **Frame-accurate timing:** Uses `Math.floor(time × fps)` to ensure consistent frame numbering (never rounds, which would be off by ±0.5 frames)
- **Precise CRT calculation:** Bridges the gap between encoding frame rate (fps in the `.mp4` file) and capture frame rate (from `.stamp.json`), using frame numbers as the stable reference

---

## Tips & Troubleshooting

### Videos won't play
- Re-encode with `python convert_videos.py` — they're likely not H.264 Baseline
- Check the browser console (F12) for specific codec errors

### Marks not saving to Excel
- Ensure `excel_helper.py` is running on the correct IP/port
- From the browser, check if the "N unsaved" button appears; click it to retry
- Verify `data.xlsx` is not open in Excel (file locks prevent writes)

### Frame numbers seem off
- Check that the stamp file's `frame_count` is accurate (must match actual frame count in the `.mp4`)
- Verify the video's encoded fps matches your `FPS_OVERRIDES` (or none, to use default 30)

### Phone can't reach the server
- Use your PC's LAN IP, not `127.0.0.1`
- Both servers (`excel_helper.py` and `http.server`) must be reachable from the phone
- Check firewall settings if the phone is on a different WiFi network

---

## Git Branch & GitHub Account Organization

⚠️ **IMPORTANT: This repository has two remotes — make sure you're pushing to the correct one!**

### Your account (origin) — Primary development
- **GitHub:** https://github.com/VenujaNilaweera/CRT-Study-Video
- **Branch:** `gui` (your active branch for new features)
- **Push to:** `origin` (this is your repository)
- Use this for: Your own development, GUI improvements, and original code

### Pansilu's account (upstream) — Reference/original
- **GitHub:** https://github.com/PansiluHarshan/CRT-Study-Video
- **Branch:** `main` (do not push here)
- **Pull from:** `upstream` only (to stay synced if needed)
- Use this for: Reviewing original code, understanding architecture

### Key Commands

**To check which remote you're using:**
```bash
git remote -v
```

**To push ONLY to your account (origin/gui):**
```bash
git push origin gui
```

**To pull Pansilu's changes (read-only):**
```bash
git fetch upstream
git diff upstream/main  # review before merging
```

**Avoid accidentally pushing to upstream:**
```bash
git remote set-url --push upstream DISABLE
```

**Your active branches:**
- `gui` — Your current branch (push here)
- `main` — Synced from origin (don't edit this)

---

## Files Reference

- **index.html** (~850 lines) — Complete web app: login form, collections browser, video player with frame-stepping, marking UI, keyboard shortcuts, session persistence, annotation queuing, CSV export
- **styles.css** — CSS custom properties for theme colors; responsive grid, flexbox layouts, dark/light mode support
- **excel_helper.py** (~160 lines) — HTTP server (port 8787) that receives annotations via POST and appends them to the Excel workbook
- **convert_videos.py** — Re-encodes videos to H.264 Baseline for browser playback (not included in this README; add as needed)


