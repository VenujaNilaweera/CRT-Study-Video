# CRT Perception Study

A small web app for collecting **when medical professionals perceive the colour
return** in capillary-refill-test (CRT) clips. Each participant logs in, watches
a clip, and presses **one button** the instant they see the colour come back.
The app records the exact **time and video frame** of that click and saves it to
`data.xlsx`, so many professionals' judgements can be pooled into a consensus
refill threshold.

## Running it

1. **Start the Excel saver** (writes each click into `data.xlsx`):
   ```bash
   python excel_helper.py
   ```
2. **Serve the site** (so the browser can list and load the videos):
   ```bash
   python -m http.server 8899
   ```
3. Open <http://127.0.0.1:8899/index.html> in a browser.

> **Don't use a live-reload server** (VS Code "Live Server", `live-server`, etc.)
> for this. Every saved click makes `excel_helper.py` write `data.xlsx` inside
> this folder; a live-reload server sees that file change and refreshes the page.
> The login and current video are now remembered across refreshes, so it no
> longer kicks you back to login — but the video still restarts on every save,
> which is annoying. Plain `python -m http.server` doesn't watch files, so use
> that.

## Adding videos

Drop clips into the **`videos/`** folder — the app auto-discovers everything in
there, so no code editing is needed. Reload the page and the new clips appear.

> **Important:** a file ending in `.mp4` is *not* guaranteed to play in a
> browser. Screen recorders often produce MPEG-4 Part 2 / DivX / HEVC, which
> browsers refuse to play. To be safe, after pasting clips run:
>
> ```bash
> python convert_videos.py
> ```
>
> This re-encodes anything that isn't already browser-safe **H.264 (Baseline)**,
> keeping the frame rate and frame count identical (so frame numbers stay
> valid). Originals are backed up in `videos/_original_backup/`.

Want separate folders/collections? Edit `VIDEO_FOLDERS` near the top of
`index.html` and add more `{ id, name, path }` entries.

Frame rate defaults to **30 fps** (used to turn the marked time into a frame
number). If some clips run at another rate, add them to `FPS_OVERRIDES` in
`index.html`.

## Data output

`data.xlsx` (sheet **Annotations**) gets one row per saved click:

| Name | Role | Age Group | Collection | Video # | Video Title | Time (s) | FPS | Frame # | Submitted At |
|------|------|-----------|------------|---------|-------------|----------|-----|---------|--------------|

`Time (s)` is the clip time of the click; `Frame # = round(Time × FPS)`. Each
participant can also **Export log** to CSV from the header.

## Files

- `index.html` — the whole app (login → collections → videos → player). Dark
  mode, responsive phone/tablet/desktop, retry + prev/next navigation.
- `styles.css` — theme tokens and layout.
- `excel_helper.py` — tiny local server (port 8787) that appends clicks to `data.xlsx`.
- `convert_videos.py` — makes pasted clips browser-safe H.264.
- `videos/` — put your clips here.
