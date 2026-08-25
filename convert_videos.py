"""
convert_videos.py  —  make every clip in videos/ playable in the browser.

Why you need this
-----------------
A file ending in ".mp4" is NOT guaranteed to play in a web browser. Screen
recorders often save MPEG-4 Part 2 / DivX / HEVC, which browsers refuse to
play. This script re-encodes anything that isn't already browser-safe H.264
(Constrained Baseline) so the study site can show it.

What it does
------------
  * Looks at every video sitting directly inside  videos/
  * Skips clips that are already Baseline H.264 (nothing to do)
  * For the rest: moves the original into  videos/_original_backup/
    and writes a browser-safe H.264 copy in its place (same filename)
  * Keeps the frame rate and frame count identical, so the marked
    frame numbers stay valid.

Workflow
--------
  1. Drop your .mp4 clips into the  videos/  folder.
  2. Run:   python convert_videos.py
  3. Reload the site — the clips appear automatically.

Requires ffmpeg + ffprobe on PATH (https://ffmpeg.org/download.html).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIDEOS_DIR = ROOT / "videos"
BACKUP_DIR = VIDEOS_DIR / "_original_backup"

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".ogv", ".wmv", ".flv"}
# H.264 profiles a browser (and the study preview) will reliably play.
SAFE_PROFILES = {"Constrained Baseline", "Baseline"}


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(path: Path) -> tuple[str, str]:
    """Return (codec_name, profile) of the first video stream, or ('', '')."""
    result = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,profile",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    codec = lines[0] if len(lines) > 0 else ""
    profile = lines[1] if len(lines) > 1 else ""
    return codec, profile


def is_browser_safe(path: Path) -> bool:
    codec, profile = probe(path)
    return codec == "h264" and profile in SAFE_PROFILES


def convert(src_original: Path, dest: Path) -> bool:
    """Transcode src_original -> dest as Baseline H.264 mp4."""
    result = run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src_original),
        "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
        "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        # All-intra: a keyframe on EVERY frame so the browser can seek to any
        # single frame exactly (frame stepping + auto-seek to the release frame).
        # Clips are short, so the larger file size is a fine trade for precision.
        "-g", "1", "-keyint_min", "1", "-sc_threshold", "0",
        "-vsync", "cfr", "-movflags", "+faststart",
        "-an",  # study clips have no audio; drop it. Remove this line to keep audio.
        str(dest),
    ])
    if result.returncode != 0:
        print("    ffmpeg error:", result.stderr.strip().splitlines()[-1] if result.stderr else "unknown")
        return False
    return True


def main() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ERROR: ffmpeg / ffprobe not found on PATH. Install from https://ffmpeg.org/download.html")
        return

    if not VIDEOS_DIR.exists():
        print(f"No videos folder found at {VIDEOS_DIR}")
        return

    BACKUP_DIR.mkdir(exist_ok=True)

    clips = sorted(
        p for p in VIDEOS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )
    if not clips:
        print("No video files found in videos/. Drop your clips there first.")
        return

    converted = skipped = failed = 0
    for clip in clips:
        if is_browser_safe(clip):
            print(f"[skip] {clip.name}  (already browser-safe H.264)")
            skipped += 1
            continue

        print(f"[conv] {clip.name}  ...")
        backup = BACKUP_DIR / clip.name
        # Always target an .mp4 output next to the original name.
        dest = clip.with_suffix(".mp4")

        # Move the original out of the way (into backup) before writing the copy.
        if backup.exists():
            backup = BACKUP_DIR / f"{clip.stem}_{clip.stat().st_mtime_ns}{clip.suffix}"
        shutil.move(str(clip), str(backup))

        if convert(backup, dest):
            print(f"        -> {dest.name}  (original saved in _original_backup/)")
            converted += 1
        else:
            # Roll back so we never lose the file on a failed encode.
            shutil.move(str(backup), str(clip))
            print(f"        FAILED, left {clip.name} untouched")
            failed += 1

    print(f"\nDone. {converted} converted, {skipped} already fine, {failed} failed.")
    if converted:
        print("Reload the study site to see the updated clips.")


if __name__ == "__main__":
    main()
