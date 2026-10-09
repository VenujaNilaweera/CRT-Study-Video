"""
studio.py - CRT Study Studio

A dark desktop tool that takes raw pump recordings all the way to the live
study site in one pass:

    pick a folder  ->  add the release flash  ->  re-encode for the browser
                   ->  upload to Supabase Storage  ->  create the database rows
                   ->  group the clips into collections

Everything reports live progress, so a batch of 60 clips is one click instead
of sixty manual uploads.

WHAT IT NEEDS
-------------
  * ffmpeg + ffprobe on PATH          (https://ffmpeg.org/download.html)
  * pip install opencv-python requests
  * A Supabase SERVICE ROLE key. The site's public key is deliberately
    read-only (it cannot write videos, collections or storage), so uploading
    needs the service key. Paste it once in Settings; it is saved to
    studio_config.json, which is git-ignored. NEVER commit that file and never
    put the service key in index.html - it would give anyone full database
    access.

HOW THE TIMING METADATA WORKS (this is load-bearing - do not "simplify" it)
--------------------------------------------------------------------------
Each recording has a sidecar <name>.stamp.json describing the REAL capture:
frame_count, recording_duration_s, fps and stamped_frame (the frame where
pressure was released). The encoded .mp4 plays at a different rate, so the two
timelines are bridged by FRAME NUMBER, never by seconds:

    real CRT = (marked_frame - stamped_frame) / (frame_count / recording_duration_s)

The upload keeps frame_count identical to the source so those frame numbers
stay valid, and encodes all-intra (-g 1) so the browser can seek to any single
frame exactly.

SITE NAMES AND TAKEN-DOWN CLIPS ON THIS LAPTOP
----------------------------------------------
After every upload, and whenever "Sync names & folders" is pressed, each
recording's .stamp.json is given a "site" block holding the name the site
uses for it ("CRT 1-07") and the clip's uuid - the recording itself is never
renamed. Clips taken down on the site are moved into a taken_down subfolder
(and back out if put back), and clip_names.csv in the working folder lists
every site name next to its recording. See crt_local.py.

PARTICIPANT IDS
---------------
This tool never creates participant IDs. Every mark is given its Study ID by
the database itself (supabase_participant_ids.sql).
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import requests

import crt_local

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:                                    # flash overlay is optional
    HAS_CV2 = False

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "studio_config.json"              # git-ignored (holds the service key)
LEDGER_NAME = "uploaded_videos.txt"                    # plain-text record, lives in the working folder

DEFAULT_SRC = r"D:\project\Reserch\resorces\pump_recordings"
DEFAULT_WORK = r"D:\project\Reserch\resorces\editedvideos"

SUPABASE_URL = "https://nsmdjwgurzoyfkxatgzz.supabase.co"
STORAGE_BUCKET = "vids"

FLASH_FRAMES = 3
FLASH_COLOR = (0, 255, 0)                              # BGR green
FLASH_ALPHA = 0.5

# ---- dark palette (mirrors the study site) ----
BG        = "#0f1416"
SURFACE   = "#161d20"
SURFACE_2 = "#1d262a"
LINE      = "#2a3639"
INK       = "#e8eef0"
INK_DIM   = "#93a4a9"
TEAL      = "#38b7a3"
TEAL_DARK = "#2b8f80"
GREEN     = "#2ecc71"
RED       = "#e8604f"
AMBER     = "#e0a53d"

STATUS_COLORS = {
    "waiting":   AMBER,                                # new - still to upload
    "skipped":   INK,                                  # already on the site
    "encoding":  AMBER,
    "uploading": AMBER,
    "saving":    AMBER,
    "done":      GREEN,
    "failed":    RED,
}


# ===========================================================================
#  Data
# ===========================================================================
@dataclass
class Clip:
    """One recording plus the stamp metadata that makes its frames meaningful."""
    video: Path
    stamp: Path
    stamped_frame: int | None = None
    frame_count: int | None = None
    recording_duration_s: float | None = None
    capture_fps: float | None = None
    stamp_time_s: float | None = None
    trigger_source: str | None = None
    post_stamp_tail_s: float | None = None
    status: str = "waiting"
    note: str = ""
    collection_id: str = ""
    video_number: int = 0
    # What this recording is CALLED on the study site ("CRT Test 07"). A
    # participant reporting a problem quotes that name, never the source
    # filename, so the two have to be visible side by side here.
    site_title: str = ""

    @property
    def name(self) -> str:
        return self.video.name

    @classmethod
    def load(cls, video: Path, stamp: Path) -> "Clip":
        clip = cls(video=video, stamp=stamp)
        try:
            data = json.loads(stamp.read_text(encoding="utf-8"))
        except Exception as exc:
            clip.status, clip.note = "failed", f"unreadable stamp: {exc}"
            return clip
        clip.stamped_frame = data.get("stamped_frame")
        clip.frame_count = data.get("frame_count")
        clip.recording_duration_s = data.get("recording_duration_s")
        clip.capture_fps = data.get("fps")
        clip.stamp_time_s = data.get("stamp_time_s")
        clip.trigger_source = data.get("trigger_source")
        clip.post_stamp_tail_s = data.get("post_stamp_tail_s")
        if clip.stamped_frame is None:
            clip.status, clip.note = "failed", "stamp.json has no stamped_frame"
        return clip


@dataclass
class Settings:
    src_dir: str = DEFAULT_SRC
    work_dir: str = DEFAULT_WORK
    service_key: str = ""
    per_collection: int = 10
    add_flash: bool = True
    skip_existing: bool = True
    collection_icon: str = "\U0001F4C1"
    extras: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        if CONFIG_PATH.exists():
            try:
                return cls(**{**cls().__dict__, **json.loads(CONFIG_PATH.read_text("utf-8"))})
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")


# ===========================================================================
#  ffmpeg helpers
# ===========================================================================
def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def have_ffmpeg() -> bool:
    try:
        return _run(["ffmpeg", "-version"]).returncode == 0
    except FileNotFoundError:
        return False


def probe(path: Path) -> tuple[int, float]:
    """Return (frame_count, duration_seconds) of the encoded file."""
    res = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-count_frames", "-show_entries", "stream=nb_read_frames,duration",
        "-of", "json", str(path),
    ])
    try:
        st = json.loads(res.stdout)["streams"][0]
        return int(st.get("nb_read_frames") or 0), float(st.get("duration") or 0)
    except Exception:
        return 0, 0.0


def encode_web(src: Path, dest: Path) -> None:
    """
    Re-encode to browser-safe H.264, ALL-INTRA.

    -g 1 -keyint_min 1 -sc_threshold 0 puts a keyframe on every frame. Without
    it libx264 emits ~3 keyframes for a short clip and the browser physically
    cannot seek between them: currentTime snaps to a distant keyframe, which
    breaks frame stepping and the release marker. Clips are seconds long, so
    the extra size is a fine trade for exact seeking.
    """
    tmp = dest.with_name(dest.stem + ".tmp.mp4")
    res = _run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
        "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-g", "1", "-keyint_min", "1", "-sc_threshold", "0",
        "-vsync", "cfr", "-movflags", "+faststart", "-an",
        str(tmp),
    ])
    if res.returncode != 0:
        tmp.unlink(missing_ok=True)
        tail = (res.stderr or "").strip().splitlines()
        raise RuntimeError(tail[-1] if tail else "ffmpeg failed")
    tmp.replace(dest)


def write_flashed(src: Path, dest: Path, stamped_frame: int) -> None:
    """Tint FLASH_FRAMES frames green from the release frame, then web-encode."""
    if not HAS_CV2:
        encode_web(src, dest)
        return
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError("cannot open source video")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frames, idx = [], 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if stamped_frame <= idx <= stamped_frame + FLASH_FRAMES - 1:
            overlay = frame.copy()
            overlay[:] = FLASH_COLOR
            frame = cv2.addWeighted(overlay, FLASH_ALPHA, frame, 1 - FLASH_ALPHA, 0)
        frames.append(frame)
        idx += 1
    cap.release()
    if not frames:
        raise RuntimeError("no frames decoded")

    raw = dest.with_name(dest.stem + ".raw.mp4")
    h, w = frames[0].shape[:2]
    writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for f in frames:
        writer.write(f)
    writer.release()
    try:
        encode_web(raw, dest)
    finally:
        raw.unlink(missing_ok=True)


# ===========================================================================
#  Ledger - the plain-text record of what has already been published
# ===========================================================================
#  Drop new recordings into the source folder whenever you like: anything
#  already listed here is skipped outright, so it is never re-encoded and
#  never uploaded a second time. Delete a line to force that clip through
#  again (handy if an upload was replaced or a clip was re-recorded).
LEDGER_HEADER = (
    "# CRT Study Studio - clips already processed and published.\n"
    "# Delete a line to force that clip to be re-encoded and re-uploaded.\n"
    "# name\tframes\tcollection\tnumber\tuploaded_at\n"
)


def ledger_file(work_dir: str | Path) -> Path:
    return Path(work_dir) / LEDGER_NAME


def load_ledger(work_dir: str | Path) -> dict[str, str]:
    """Map of clip filename -> the full record line (comments ignored)."""
    path = ledger_file(work_dir)
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out[line.split("\t")[0].strip()] = line
    return out


def append_ledger(work_dir: str | Path, name: str, frames: int,
                  collection: str, number: int) -> None:
    from datetime import datetime
    path = ledger_file(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(LEDGER_HEADER, encoding="utf-8")
    stamp = datetime.now().isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{name}\t{frames}\t{collection}\t{number}\t{stamp}\n")


def is_all_intra(path: Path) -> bool:
    """
    True when every frame is a keyframe.

    ffprobe's csv=p=0 output can carry a trailing comma on a line (e.g. "1,"),
    so take the first comma-separated field rather than comparing the raw line.
    """
    res = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "frame=key_frame", "-of", "csv=p=0", str(path),
    ])
    flags = [ln.split(",")[0].strip()
             for ln in res.stdout.splitlines() if ln.strip()]
    return bool(flags) and all(f == "1" for f in flags)


def encoded_is_reusable(out_path: Path, expected_frames: int | None) -> bool:
    """
    True when a previously encoded file can be uploaded as-is.

    A matching frame count is NOT enough. vidoeprocess.py writes into the same
    working folder without the all-intra flags, so a file with the right frame
    count can still carry only a couple of keyframes. Uploading that would make
    backward frame stepping decode from the start of the clip every time, which
    on a phone is the difference between instant and visibly laggy. Anything
    that is not all-intra is re-encoded.
    """
    if not out_path.exists() or out_path.stat().st_size == 0:
        return False
    frames, duration = probe(out_path)
    if not frames or not duration:
        return False
    if expected_frames is not None and frames != expected_frames:
        return False
    return is_all_intra(out_path)


# ===========================================================================
#  Supabase
# ===========================================================================
class Supabase:
    def __init__(self, key: str):
        self.key = key.strip()
        self.rest = f"{SUPABASE_URL}/rest/v1"
        self.storage = f"{SUPABASE_URL}/storage/v1"

    @property
    def headers(self) -> dict:
        return {"apikey": self.key, "Authorization": f"Bearer {self.key}"}

    def check(self) -> tuple[bool, str]:
        """Confirm the key can actually WRITE (the public key cannot)."""
        try:
            r = requests.get(f"{self.rest}/videos?select=id&limit=1",
                             headers=self.headers, timeout=20)
        except Exception as exc:
            return False, f"cannot reach Supabase: {exc}"
        if r.status_code == 401:
            return False, "key rejected (401). Paste the service_role key."
        if not r.ok:
            return False, f"unexpected response {r.status_code}"
        # A no-op update proves write permission without changing any data.
        probe_id = "00000000-0000-0000-0000-000000000000"
        w = requests.patch(f"{self.rest}/videos?id=eq.{probe_id}",
                           headers={**self.headers, "Content-Type": "application/json"},
                           json={"title": "probe"}, timeout=20)
        if w.status_code in (401, 403):
            return False, "this key is read-only - use the service_role key"
        return True, "service key OK"

    def existing_paths(self) -> set[str]:
        r = requests.get(f"{self.rest}/videos?select=storage_path",
                         headers=self.headers, timeout=30)
        r.raise_for_status()
        return {row["storage_path"] for row in r.json() if row.get("storage_path")}

    def collections(self) -> list[dict]:
        r = requests.get(f"{self.rest}/collections?select=*&order=sort_order",
                         headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def upsert_collection(self, cid: str, name: str, icon: str, sort_order: int) -> None:
        requests.post(
            f"{self.rest}/collections",
            headers={**self.headers, "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json={"id": cid, "name": name, "icon": icon, "sort_order": sort_order},
            timeout=30,
        ).raise_for_status()

    def upload_file(self, path: Path, dest_name: str) -> None:
        url = f"{self.storage}/object/{STORAGE_BUCKET}/{dest_name}"
        data = path.read_bytes()
        head = {**self.headers, "Content-Type": "video/mp4",
                "x-upsert": "true", "cache-control": "3600"}
        r = requests.post(url, headers=head, data=data, timeout=600)
        if r.status_code in (400, 409):                # already there -> replace
            r = requests.put(url, headers=head, data=data, timeout=600)
        if not r.ok:
            raise RuntimeError(f"storage upload failed ({r.status_code}): {r.text[:200]}")

    def insert_video(self, row: dict, upsert: bool = True) -> dict:
        # upsert=False for a replacement. merge-duplicates would quietly UPDATE
        # a conflicting row instead of failing, and the row it would land on is
        # the retired one we just took down - rewriting its storage_path to the
        # new file and handing the bad clip's marks the good clip's footage.
        prefer = ("resolution=merge-duplicates,return=representation" if upsert
                  else "return=representation")
        r = requests.post(
            f"{self.rest}/videos",
            headers={**self.headers, "Content-Type": "application/json",
                     "Prefer": prefer},
            json=row, timeout=60,
        )
        if not r.ok:
            raise RuntimeError(f"row insert failed ({r.status_code}): {r.text[:200]}")
        try:
            body = r.json()
            return body[0] if isinstance(body, list) and body else (body or {})
        except Exception:
            return {}

    # ---------------- library management ----------------
    # Everything below exists so a clip that turns out to be wrong can be taken
    # off the site. See supabase_retire_clips.sql for WHY none of it deletes.

    def library(self) -> list[dict]:
        """Every published clip, with how many marks it carries.

        Prefers the crt_clip_usage view (which joins the mark counts). If that
        view is missing - the migration has not been run - fall back to the
        plain videos table so the window still opens and can still tell you
        what is there; it just cannot show mark counts, so it treats every clip
        as if it had marks, which is the cautious way round.
        """
        r = requests.get(
            f"{self.rest}/crt_clip_usage?select=*&order=collection_id,video_number",
            headers=self.headers, timeout=30)
        if r.ok:
            rows = r.json()
            for row in rows:
                row["marks_known"] = True
            return rows

        r = requests.get(
            f"{self.rest}/videos?select=*&order=collection_id,video_number",
            headers=self.headers, timeout=30)
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            row["marks"] = None            # unknown, not zero
            row["marks_known"] = False
        return rows

    def set_active(self, video_id: str, active: bool, reason: str = "") -> None:
        payload: dict = {"active": active}
        if not active and reason:
            payload["retired_reason"] = reason[:300]
        r = requests.patch(
            f"{self.rest}/videos?id=eq.{video_id}",
            headers={**self.headers, "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            json=payload, timeout=30)
        if r.status_code in (400, 404) and "active" in r.text:
            raise RuntimeError(
                "this project has no 'active' column yet - run "
                "supabase_retire_clips.sql in the Supabase SQL editor first")
        if not r.ok:
            raise RuntimeError(f"could not update the clip ({r.status_code}): {r.text[:200]}")

    def delete_video(self, video_id: str) -> None:
        """Only ever called for a clip with zero marks - see the caller.

        annotations.video_id cascades on delete, so calling this on a clip that
        HAS marks destroys them with no warning and no way back.
        """
        r = requests.delete(f"{self.rest}/videos?id=eq.{video_id}",
                            headers={**self.headers, "Prefer": "return=minimal"},
                            timeout=30)
        if not r.ok:
            raise RuntimeError(f"delete failed ({r.status_code}): {r.text[:200]}")

    def remove_file(self, dest_name: str) -> None:
        requests.delete(f"{self.storage}/object/{STORAGE_BUCKET}/{dest_name}",
                        headers=self.headers, timeout=60)

    def storage_names(self) -> set[str]:
        """Filenames already in the bucket, so a replacement can pick a free one."""
        r = requests.post(f"{self.storage}/object/list/{STORAGE_BUCKET}",
                          headers={**self.headers, "Content-Type": "application/json"},
                          json={"prefix": "", "limit": 10000}, timeout=60)
        if not r.ok:
            return set()
        try:
            return {row["name"] for row in r.json() if row.get("name")}
        except Exception:
            return set()


# ===========================================================================
#  Site names and taken-down clips on this laptop (see crt_local.py)
# ===========================================================================
def label_stamps(row: dict, *stamps: Path) -> None:
    """Write the site's name for a just-saved clip into its stamp file(s).
    Best effort: a stamp that cannot be written never fails an upload."""
    for stamp in stamps:
        try:
            crt_local.write_site_info(Path(stamp), row)
        except Exception:
            pass


def run_sync(sb: "Supabase", s: "Settings", log) -> None:
    """Label every local recording with its site name, move taken-down clips
    into taken_down/ (and put-back ones out again), rewrite clip_names.csv."""
    try:
        rows = sb.library()
        rep = crt_local.sync_local(rows, s.src_dir, s.work_dir)
    except Exception as exc:
        log(f"Could not sync names & folders: {exc}", "error")
        return
    for msg, tone in rep.lines():
        log(msg, tone)


# ===========================================================================
#  The pipeline (runs off the UI thread)
# ===========================================================================
class Pipeline(threading.Thread):
    def __init__(self, clips: list[Clip], settings: Settings, events: queue.Queue):
        super().__init__(daemon=True)
        self.clips, self.s, self.q = clips, settings, events
        self.cancel = threading.Event()

    def emit(self, kind: str, **payload):
        self.q.put({"kind": kind, **payload})

    def log(self, msg: str, tone: str = "info"):
        self.emit("log", msg=msg, tone=tone)

    def set_status(self, clip: Clip, status: str, note: str = ""):
        clip.status, clip.note = status, note
        self.emit("row", clip=clip)

    def run(self):
        try:
            self._run()
        except Exception as exc:                      # never die silently
            self.log(f"Fatal: {exc}", "error")
            self.emit("finished", ok=False)

    def _run(self):
        sb = Supabase(self.s.service_key)
        self.log("Checking Supabase credentials...")
        ok, msg = sb.check()
        if not ok:
            self.log(msg, "error")
            self.emit("finished", ok=False)
            return
        self.log(msg, "good")

        work = Path(self.s.work_dir)
        work.mkdir(parents=True, exist_ok=True)

        # Two independent records of "already done": the local ledger and the
        # live database. Either one is enough to skip a clip, so a wiped
        # ledger or a fresh machine still cannot create duplicates.
        ledger = load_ledger(work)
        existing: set[str] = set()
        if self.s.skip_existing:
            existing |= set(ledger)
            try:
                existing |= sb.existing_paths()
            except Exception as exc:
                self.log(f"Could not read the site's clip list ({exc}); "
                         f"using the local ledger only.", "warn")
        if ledger:
            self.log(f"Ledger lists {len(ledger)} clip(s) already published "
                     f"({ledger_file(work).name}).")
        if existing:
            self.log(f"{len(existing)} clip(s) already done - those are skipped.")

        # Continue collection numbering after whatever is already live.
        cols = sb.collections()
        used = {c["id"] for c in cols}
        next_n = 1
        while f"col{next_n}" in used:
            next_n += 1
        counts = {c["id"]: 0 for c in cols}
        try:
            r = requests.get(f"{sb.rest}/videos?select=collection_id",
                             headers=sb.headers, timeout=30)
            for row in r.json():
                counts[row["collection_id"]] = counts.get(row["collection_id"], 0) + 1
        except Exception:
            pass

        todo = [c for c in self.clips if c.status != "failed"]
        pending = [c for c in todo if c.video.name not in existing]
        for c in todo:
            if c.video.name in existing:
                self.set_status(c, "skipped", "already on the site")

        if not pending:
            self.log("Nothing new to upload.", "good")
            self.emit("finished", ok=True)
            return

        # Fill the last collection up to per_collection, then open new ones.
        cur_id = None
        for c in sorted(cols, key=lambda x: x.get("sort_order", 0)):
            if counts.get(c["id"], 0) < self.s.per_collection:
                cur_id, cur_count = c["id"], counts.get(c["id"], 0)
                break
        if cur_id is None:
            cur_id, cur_count = f"col{next_n}", 0
            sb.upsert_collection(cur_id, f"Collection {next_n:02d}",
                                 self.s.collection_icon, next_n - 1)
            self.log(f"Created {cur_id}")
            next_n += 1

        total = len(pending)
        self.log(f"Processing {total} clip(s)...")
        done = failed = 0

        for i, clip in enumerate(pending, 1):
            if self.cancel.is_set():
                self.log("Cancelled.", "warn")
                break

            if cur_count >= self.s.per_collection:     # roll into a new collection
                cur_id, cur_count = f"col{next_n}", 0
                sb.upsert_collection(cur_id, f"Collection {next_n:02d}",
                                     self.s.collection_icon, next_n - 1)
                self.log(f"Created {cur_id}")
                next_n += 1

            self.emit("progress", value=(i - 1) / total * 100,
                      text=f"{i} of {total}  -  {clip.name}")
            try:
                out = work / clip.video.name

                # Re-use an encode from a previous run instead of spending
                # minutes producing a byte-for-byte equivalent file.
                self.set_status(clip, "encoding")
                if encoded_is_reusable(out, clip.frame_count):
                    self.log(f"  {clip.name}: reusing the existing encode")
                elif self.s.add_flash and clip.stamped_frame is not None:
                    write_flashed(clip.video, out, clip.stamped_frame)
                else:
                    encode_web(clip.video, out)

                frames, duration = probe(out)
                if not frames or not duration:
                    raise RuntimeError("could not probe the encoded file")
                # frame_count must match the stamp, or every frame number the
                # study records would be measured against the wrong timeline.
                if clip.frame_count and frames != clip.frame_count:
                    self.log(f"  {clip.name}: {frames} frames encoded vs "
                             f"{clip.frame_count} in stamp.json - using encoded count",
                             "warn")

                # Copy the sidecar next to the encoded clip for local reference.
                try:
                    (work / clip.stamp.name).write_bytes(clip.stamp.read_bytes())
                except Exception:
                    pass

                self.set_status(clip, "uploading")
                sb.upload_file(out, clip.video.name)

                self.set_status(clip, "saving")
                cur_count += 1
                number = cur_count
                site_title = site_name(cur_id, number)
                real_fps = (clip.frame_count / clip.recording_duration_s
                            if clip.frame_count and clip.recording_duration_s else clip.capture_fps)
                saved = sb.insert_video({
                    "collection_id": cur_id,
                    "video_number": number,
                    "title": site_title,
                    "storage_path": clip.video.name,
                    "encoded_fps": round(frames / duration, 3),
                    "frame_count": clip.frame_count or frames,
                    "stamped": clip.stamped_frame is not None,
                    "stamped_frame": clip.stamped_frame,
                    "recording_duration_s": clip.recording_duration_s,
                    "capture_fps": round(real_fps, 3) if real_fps else None,
                    "stamp_time_s": clip.stamp_time_s,
                    "trigger_source": clip.trigger_source,
                    "post_stamp_tail_s": clip.post_stamp_tail_s,
                })

                # Record it only once the row is safely in the database, so a
                # failure part-way through never marks a clip as published.
                append_ledger(work, clip.video.name, frames, cur_id, number)
                # And write the site's name for it into the stamp files here
                # (the source one and the copy beside the encode).
                label_stamps(saved, clip.stamp, work / clip.stamp.name)

                clip.collection_id, clip.video_number = cur_id, number
                clip.site_title = site_title
                self.set_status(clip, "done", f"{cur_id} #{number}")
                # Log both names together: this line is what you search when a
                # participant reports "CRT Test 07 will not play".
                self.log(f"  {clip.name} -> {site_title}  ({cur_id} #{number}, "
                         f"{frames} frames)", "good")
                done += 1
            except Exception as exc:
                self.set_status(clip, "failed", str(exc)[:120])
                self.log(f"  {clip.name}: {exc}", "error")
                failed += 1

            self.emit("progress", value=i / total * 100,
                      text=f"{i} of {total} complete")

        self.log(f"Finished - {done} uploaded, {failed} failed.",
                 "good" if not failed else "warn")
        if done:
            run_sync(sb, self.s, self.log)
        if done:
            self.log("Reload the study site to see them (hard-refresh).", "good")
        self.emit("finished", ok=failed == 0)


# ===========================================================================
#  Replacing one clip (runs off the UI thread)
# ===========================================================================
def site_name(collection_id: str, number: int) -> str:
    """What participants see a clip called on the study site.

    Clip numbers restart at 1 in every collection, so the number on its own is
    not unique: "CRT Test 07" existed once per collection, and a participant
    reporting that name could have meant any of them. Naming the collection too
    makes the name point at exactly one clip - col1 #7 is "CRT 1-07", col2 #7
    is "CRT 2-07". index.html builds the same string when a row has no title.
    """
    digits = "".join(ch for ch in str(collection_id or "") if ch.isdigit())
    return f"CRT {digits or collection_id}-{number:02d}"


def free_storage_name(original: str, taken: set[str]) -> str:
    """A name for a re-uploaded file that cannot collide with the old one.

    Uploads are sent with x-upsert, so writing a corrected file under the name
    the bad one already uses would OVERWRITE it - and the retired row would
    then point at the new content, quietly attaching the bad clip's marks to
    the good clip's footage. A fresh name keeps the two apart for good.
    """
    stem, dot, ext = original.rpartition(".")
    if not dot:
        stem, ext = original, "mp4"
    n = 2
    while f"{stem}__r{n}.{ext}" in taken:
        n += 1
    return f"{stem}__r{n}.{ext}"


class Replacer(threading.Thread):
    """Swap a corrected recording in for a clip already on the site.

    The old row is never edited and never deleted: it is retired, keeping its
    marks. The corrected file goes up as a NEW row under a NEW storage name,
    inheriting the retired clip's collection and number so the study's clip
    numbering does not shift underneath anyone.
    """

    def __init__(self, row: dict, clip: "Clip", settings: Settings,
                 reason: str, events: queue.Queue):
        super().__init__(daemon=True)
        self.row, self.clip, self.s, self.reason, self.q = row, clip, settings, reason, events

    def log(self, msg: str, tone: str = "info"):
        self.q.put({"kind": "log", "msg": msg, "tone": tone})

    def run(self):
        try:
            self._run()
        except Exception as exc:
            self.log(f"Replace failed: {exc}", "error")
            self.q.put({"kind": "replaced", "ok": False})

    def _run(self):
        sb = Supabase(self.s.service_key)
        ok, msg = sb.check()
        if not ok:
            raise RuntimeError(msg)

        work = Path(self.s.work_dir or DEFAULT_WORK)
        work.mkdir(parents=True, exist_ok=True)
        clip = self.clip

        self.log(f"Encoding {clip.name} ...")
        out = work / clip.video.name
        if self.s.add_flash and clip.stamped_frame is not None:
            write_flashed(clip.video, out, clip.stamped_frame)
        else:
            encode_web(clip.video, out)

        frames, duration = probe(out)
        if not frames or not duration:
            raise RuntimeError("could not probe the encoded file")

        # A different object name every time, so the retired row keeps pointing
        # at the footage its marks were actually made against.
        taken = sb.storage_names() | sb.existing_paths()
        dest = free_storage_name(self.row.get("storage_path") or clip.video.name, taken)
        self.log(f"Uploading as {dest} ...")
        sb.upload_file(out, dest)

        number = self.row.get("video_number")
        real_fps = (clip.frame_count / clip.recording_duration_s
                    if clip.frame_count and clip.recording_duration_s else clip.capture_fps)
        new_row = {
            "collection_id": self.row.get("collection_id"),
            "video_number": number,
            "title": self.row.get("title") or site_name(self.row.get("collection_id"), number),
            "storage_path": dest,
            "encoded_fps": round(frames / duration, 3),
            "frame_count": clip.frame_count or frames,
            "stamped": clip.stamped_frame is not None,
            "stamped_frame": clip.stamped_frame,
            "recording_duration_s": clip.recording_duration_s,
            "capture_fps": round(real_fps, 3) if real_fps else None,
            "stamp_time_s": clip.stamp_time_s,
            "trigger_source": clip.trigger_source,
            "post_stamp_tail_s": clip.post_stamp_tail_s,
        }

        # Retire FIRST. If the insert then fails, the site is simply short one
        # clip - annoying but harmless. The other order could leave two live
        # rows on the same number, which the queue would serve as two clips.
        self.log("Retiring the old version ...")
        sb.set_active(self.row["id"], False, self.reason or "replaced with a corrected upload")

        try:
            saved = sb.insert_video(new_row, upsert=False)
        except RuntimeError as exc:
            # Some schemas put a unique index on (collection_id, video_number).
            # Reuse is then impossible, so fall back to a fresh number rather
            # than leaving the clip missing, and say so plainly.
            if "23505" in str(exc) or "duplicate" in str(exc).lower():
                same = [r.get("video_number") or 0 for r in sb.library()
                        if r.get("collection_id") == self.row.get("collection_id")]
                nxt = 1 + max(same or [0])
                self.log(f"This project does not allow reusing clip numbers - "
                         f"the replacement went in as #{nxt}, not #{number}.", "warn")
                new_row["video_number"] = nxt
                new_row["title"] = site_name(self.row.get("collection_id"), nxt)
                saved = sb.insert_video(new_row, upsert=False)
            else:
                raise

        label_stamps({**new_row, **(saved or {})}, clip.stamp, work / clip.stamp.name)
        self.log(f"Replaced clip #{number}. The old version keeps its marks and "
                 f"stays in the table, retired.", "good")
        # Moves the old recording into taken_down/ - but only once the new
        # one's stamp names the new clip, so a file corrected in place stays.
        run_sync(sb, self.s, self.log)
        self.log("Hard-refresh the study site to see the new file.", "good")
        self.q.put({"kind": "replaced", "ok": True})


# ===========================================================================
#  GUI
# ===========================================================================
#  One window, three areas:
#    * a tab strip - "Recordings" (this laptop) and "On the site" (what
#      participants are served), each a table you can double-click to play;
#    * a settings rail on the right;
#    * an Activity log along the bottom - resizable, timestamped, and also
#      written to studio_log.txt so nothing scrolls away for good.
LOG_PATH = ROOT / "studio_log.txt"                     # git-ignored
PUBLIC_URL = f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/"

FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"
MONO = "Consolas"
ROW_ALT = "#131a1d"                                    # zebra stripe
HOVER = "#243035"

STATUS_LABEL = {
    "waiting":   "New",
    "skipped":   "On site",
    "encoding":  "Encoding...",
    "uploading": "Uploading...",
    "saving":    "Saving...",
    "done":      "Uploaded",
    "failed":    "Failed",
}


def open_file(path: Path) -> None:
    """Open a file in the system's default app (the video player for .mp4)."""
    import os
    import sys
    if sys.platform.startswith("win"):
        os.startfile(str(path))                        # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def show_in_folder(path: Path) -> None:
    import sys
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        open_file(path.parent)


def local_files_by_clip(*folders) -> dict[str, dict[str, Path]]:
    """video_id -> {"encoded": path in the working folder, "source": original}.

    Built from the "site" blocks a sync writes into the stamp files."""
    out: dict[str, dict[str, Path]] = {}
    src_dir = Path(folders[0]) if folders and folders[0] else None
    for clip in crt_local.local_clips(*folders):
        info = (crt_local.read_stamp(clip.stamp) or {}).get(crt_local.SITE_KEY) or {}
        vid = info.get("video_id")
        if not vid or not clip.video.exists():
            continue
        kind = "source" if (src_dir and clip.home == src_dir) else "encoded"
        out.setdefault(vid, {}).setdefault(kind, clip.video)
    # The encode keeps the recording's file name, so find it in the working
    # folder even when no stamp was copied beside it.
    work_dir = Path(folders[1]) if len(folders) > 1 and folders[1] else None
    if work_dir:
        for files in out.values():
            src = files.get("source")
            if src and "encoded" not in files:
                for cand in (work_dir / src.name, work_dir / crt_local.TAKEN_DOWN_DIR / src.name):
                    if cand.exists():
                        files["encoded"] = cand
                        break
    return out


class HoverButton(tk.Button):
    """A flat button that lightens on hover - the one control style used everywhere."""
    KINDS = {
        #           bg         hover      fg         font
        "primary": (TEAL,      "#45c9b4", "#06201b", (FONT_SEMI, 10)),
        "ghost":   (SURFACE_2, HOVER,     INK,       (FONT, 10)),
        "danger":  ("#3a1f1c", "#57302b", "#ffb4a8", (FONT, 10)),
        "link":    (SURFACE,   SURFACE_2, TEAL,      (FONT, 9)),
    }

    def __init__(self, parent, text, cmd, kind="ghost", **kw):
        bg, hover, fg, font = self.KINDS[kind]
        pad = {"padx": 10, "pady": 4} if kind == "link" else {"padx": 16, "pady": 8}
        super().__init__(parent, text=text, command=cmd, bg=bg, fg=fg, font=font,
                         activebackground=hover, activeforeground=fg,
                         disabledforeground="#5b6a6e", relief="flat", bd=0,
                         highlightthickness=0, cursor="hand2", **pad, **kw)
        self._bg, self._hover = bg, hover
        self.bind("<Enter>", lambda _e: self["state"] != "disabled" and self.configure(bg=self._hover))
        self.bind("<Leave>", lambda _e: self.configure(bg=self._bg))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CRT Study Studio")
        self.configure(bg=BG)
        self.geometry("1320x860")
        self.minsize(1040, 680)

        self.settings = Settings.load()
        self.clips: list[Clip] = []
        self.events: queue.Queue = queue.Queue()
        self.pipeline: Pipeline | None = None
        self.filter = tk.StringVar(value="All")
        self.search = tk.StringVar()

        self._build_style()
        self._build_ui()
        self.after(80, self._drain)
        self.write_log("Studio started.", "info")
        if Path(self.settings.src_dir).exists():
            self.scan()

    # kept for code that still asks the app for a button
    def _btn(self, parent, text, cmd, kind="primary"):
        return HoverButton(parent, text, cmd, kind)

    # ---------- theme ----------
    def _build_style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=INK, fieldbackground=SURFACE_2,
                     borderwidth=0, font=(FONT, 10))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=SURFACE)
        st.configure("Rail.TFrame", background=SURFACE)
        st.configure("TLabel", background=BG, foreground=INK, font=(FONT, 10))
        st.configure("Dim.TLabel", background=BG, foreground=INK_DIM, font=(FONT, 9))
        st.configure("Card.TLabel", background=SURFACE, foreground=INK, font=(FONT, 10))
        st.configure("CardDim.TLabel", background=SURFACE, foreground=INK_DIM, font=(FONT, 9))
        st.configure("Title.TLabel", background=BG, foreground=INK, font=(FONT_SEMI, 17))
        st.configure("Section.TLabel", background=SURFACE, foreground=INK_DIM,
                     font=(FONT_SEMI, 8))
        st.configure("Panel.TLabel", background=SURFACE, foreground=INK, font=(FONT_SEMI, 11))
        st.configure("TEntry", fieldbackground=SURFACE_2, foreground=INK, insertcolor=INK,
                     bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=7)
        st.map("TEntry", bordercolor=[("focus", TEAL)], lightcolor=[("focus", TEAL)])
        st.configure("TCheckbutton", background=SURFACE, foreground=INK, font=(FONT, 10),
                     indicatorbackground=SURFACE_2, indicatorforeground=TEAL)
        st.map("TCheckbutton", background=[("active", SURFACE)],
               indicatorbackground=[("selected", SURFACE_2)])
        st.configure("TSpinbox", fieldbackground=SURFACE_2, foreground=INK, arrowcolor=INK,
                     bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=5)
        st.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                     foreground=INK, rowheight=30, borderwidth=0, font=(FONT, 10))
        st.configure("Treeview.Heading", background=SURFACE_2, foreground=INK_DIM,
                     relief="flat", font=(FONT_SEMI, 9), padding=(8, 7))
        st.map("Treeview.Heading", background=[("active", HOVER)])
        st.map("Treeview", background=[("selected", "#1f4a44")],
               foreground=[("selected", INK)])
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])   # no border
        st.configure("TProgressbar", background=TEAL, troughcolor=SURFACE_2,
                     bordercolor=SURFACE_2, lightcolor=TEAL, darkcolor=TEAL,
                     borderwidth=0, thickness=5)
        st.configure("Vertical.TScrollbar", background=SURFACE_2, troughcolor=SURFACE,
                     arrowcolor=INK_DIM, bordercolor=SURFACE, lightcolor=SURFACE_2,
                     darkcolor=SURFACE_2, gripcount=0, arrowsize=12)
        st.map("Vertical.TScrollbar", background=[("active", HOVER)])
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0),
                     bordercolor=BG, lightcolor=BG, darkcolor=BG)
        st.configure("TNotebook.Tab", background=BG, foreground=INK_DIM,
                     padding=(20, 10), font=(FONT_SEMI, 10), borderwidth=0,
                     bordercolor=BG, lightcolor=BG, darkcolor=BG, focuscolor=BG)
        st.map("TNotebook.Tab", background=[("selected", SURFACE), ("active", SURFACE_2)],
               foreground=[("selected", INK), ("active", INK)],
               bordercolor=[("selected", SURFACE)], lightcolor=[("selected", SURFACE)],
               expand=[("selected", (0, 0, 0, 0))])
        st.configure("TCheckbutton", indicatorcolor=SURFACE_2, indicatorrelief="flat")
        st.map("TCheckbutton", indicatorcolor=[("selected", TEAL)])
        st.configure("Seg.TRadiobutton", background=SURFACE, foreground=INK_DIM,
                     font=(FONT, 9), padding=(10, 4), indicatorsize=0)
        st.layout("Seg.TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
            ("Radiobutton.label", {"sticky": "nswe"})]})])
        st.map("Seg.TRadiobutton", background=[("selected", SURFACE_2), ("active", HOVER)],
               foreground=[("selected", INK), ("active", INK)])

    # ---------- layout ----------
    def _build_ui(self):
        # ---- header ----
        head = ttk.Frame(self, padding=(22, 16, 22, 10))
        head.pack(fill="x")
        titles = ttk.Frame(head)
        titles.pack(side="left")
        ttk.Label(titles, text="CRT Study Studio", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Process recordings and publish them to the study site",
                  style="Dim.TLabel").pack(anchor="w")
        self.chips = ttk.Frame(head)
        self.chips.pack(side="right")

        # ---- vertical split: work area above, activity log below ----
        vsplit = tk.PanedWindow(self, orient="vertical", bg=BG, sashwidth=8,
                                bd=0, sashrelief="flat", opaqueresize=True)
        vsplit.pack(fill="both", expand=True, padx=22, pady=(0, 16))

        top = ttk.Frame(vsplit)
        vsplit.add(top, stretch="always", minsize=320)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)

        self.tabs = ttk.Notebook(top)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self.rec_tab = ttk.Frame(self.tabs, style="Card.TFrame")
        self.lib_tab = LibraryPanel(self.tabs, self)
        self.tabs.add(self.rec_tab, text="  Recordings  ")
        self.tabs.add(self.lib_tab, text="  On the site  ")
        self.tabs.bind("<<NotebookTabChanged>>", self._tab_changed)
        self._build_recordings(self.rec_tab)

        rail = ttk.Frame(top, style="Rail.TFrame", padding=(18, 16, 18, 18), width=320)
        rail.grid(row=0, column=1, sticky="nsew")
        rail.grid_propagate(False)
        self._build_rail(rail)

        log_area = ttk.Frame(vsplit, style="Card.TFrame")
        vsplit.add(log_area, stretch="never", minsize=120, height=210)
        self._build_log(log_area)

    def _build_recordings(self, parent):
        bar = ttk.Frame(parent, style="Card.TFrame", padding=(14, 12, 14, 10))
        bar.pack(fill="x")
        seg = ttk.Frame(bar, style="Card.TFrame")
        seg.pack(side="left")
        for name in ("All", "New", "On site", "Failed"):
            ttk.Radiobutton(seg, text=name, value=name, variable=self.filter,
                            style="Seg.TRadiobutton",
                            command=self._fill_recordings).pack(side="left", padx=(0, 2))
        ent = ttk.Entry(bar, textvariable=self.search, width=28)
        ent.pack(side="left", padx=(14, 0))
        self._placeholder(ent, self.search, "Search name or site name")
        self.search.trace_add("write", lambda *_: self._fill_recordings())
        HoverButton(bar, "Rescan", self.scan, "ghost").pack(side="right")
        HoverButton(bar, "Show in folder", self._show_selected, "ghost").pack(side="right", padx=6)
        HoverButton(bar, "Play", self._play_selected, "primary").pack(side="right")

        ttk.Label(parent, text="Double-click a recording to play it (with the green "
                               "release flash, as participants see it, once it has been "
                               "encoded). Right-click for more.",
                  style="CardDim.TLabel").pack(anchor="w", padx=14, pady=(0, 8))

        wrap = ttk.Frame(parent, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        cols = ("name", "site", "frames", "release", "status", "note")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for cid, label, w, anchor in (
            ("name", "Recording", 260, "w"),
            ("site", "Site name", 110, "w"),
            ("frames", "Frames", 80, "center"),
            ("release", "Release frame", 110, "center"),
            ("status", "Status", 110, "w"),
            ("note", "Detail", 200, "w"),
        ):
            self.tree.heading(cid, text=label, anchor=anchor)
            self.tree.column(cid, width=w, anchor=anchor, stretch=(cid in ("name", "note")))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for status, colour in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=colour)
        self.tree.tag_configure("alt", background=ROW_ALT)
        self.tree.bind("<Double-1>", lambda e: self._play_selected())
        self.tree.bind("<Return>", lambda e: self._play_selected())
        self.tree.bind("<Button-3>", self._rec_menu)

        self.empty_lbl = ttk.Label(wrap, text="", style="CardDim.TLabel")

    def _build_rail(self, rail):
        def section(text, top=0):
            ttk.Label(rail, text=text.upper(), style="Section.TLabel").pack(
                anchor="w", pady=(top, 6))

        section("Source recordings")
        self.src_var = tk.StringVar(value=self.settings.src_dir)
        self._path_row(rail, self.src_var, self.choose_src)

        section("Working folder (encoded)", 14)
        self.work_var = tk.StringVar(value=self.settings.work_dir)
        self._path_row(rail, self.work_var, self.choose_work)

        section("Supabase service key", 14)
        self.key_var = tk.StringVar(value=self.settings.service_key)
        ttk.Entry(rail, textvariable=self.key_var, show="•").pack(fill="x")
        ttk.Label(rail, text="Saved in studio_config.json on this laptop only.",
                  style="CardDim.TLabel").pack(anchor="w", pady=(4, 0))

        section("Upload options", 14)
        row = ttk.Frame(rail, style="Card.TFrame")
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text="Clips per collection", style="Card.TLabel").pack(side="left")
        self.per_var = tk.IntVar(value=self.settings.per_collection)
        ttk.Spinbox(row, from_=1, to=100, textvariable=self.per_var, width=5).pack(side="right")
        self.flash_var = tk.BooleanVar(value=self.settings.add_flash)
        ttk.Checkbutton(rail, text="Add green release flash",
                        variable=self.flash_var).pack(anchor="w", pady=2)
        self.skip_var = tk.BooleanVar(value=self.settings.skip_existing)
        ttk.Checkbutton(rail, text="Skip clips already on the site",
                        variable=self.skip_var).pack(anchor="w", pady=2)

        spacer = ttk.Frame(rail, style="Card.TFrame")
        spacer.pack(fill="both", expand=True)

        self.go_btn = HoverButton(rail, "Process & Upload", self.start, "primary")
        self.go_btn.pack(fill="x", side="bottom")
        self.cancel_btn = HoverButton(rail, "Cancel", self.stop, "danger")
        HoverButton(rail, "Sync names & folders", self.sync_local, "ghost").pack(
            fill="x", side="bottom", pady=(0, 8))

    def _path_row(self, parent, var, choose):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
        HoverButton(row, "Browse", choose, "ghost").pack(side="left", padx=(6, 0))

    def _build_log(self, parent):
        bar = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10, 14, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Activity", style="Panel.TLabel").pack(side="left")
        self.progress_lbl = ttk.Label(bar, text="Idle", style="CardDim.TLabel")
        self.progress_lbl.pack(side="left", padx=(12, 0))
        HoverButton(bar, "Open log file", lambda: LOG_PATH.exists() and open_file(LOG_PATH),
                    "link").pack(side="right")
        HoverButton(bar, "Copy", self._copy_log, "link").pack(side="right")
        HoverButton(bar, "Clear", self._clear_log, "link").pack(side="right")
        self.progress = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=14)

        body = ttk.Frame(parent, style="Card.TFrame")
        body.pack(fill="both", expand=True, padx=(14, 4), pady=(8, 10))
        self.log = tk.Text(body, bg=SURFACE, fg=INK_DIM, relief="flat", bd=0,
                           font=(MONO, 10), wrap="word", padx=4, pady=2,
                           insertbackground=INK, highlightthickness=0, spacing1=2)
        lsb = ttk.Scrollbar(body, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        for tone, colour in (("info", INK_DIM), ("good", GREEN),
                             ("warn", AMBER), ("error", RED)):
            self.log.tag_configure(tone, foreground=colour)
        self.log.tag_configure("time", foreground="#5b6a6e")
        self.log.configure(state="disabled")

        if not have_ffmpeg():
            self.after(10, lambda: self.write_log(
                "ffmpeg/ffprobe not found on PATH - encoding will fail.", "error"))
        if not HAS_CV2:
            self.after(10, lambda: self.write_log(
                "opencv-python not installed - the green flash is disabled.", "warn"))

    def _placeholder(self, entry, var, text):
        """Grey hint text in an empty entry (tk has none built in)."""
        def show(_e=None):
            if not var.get():
                entry.configure(foreground=INK_DIM)
                entry.insert(0, text)
                entry._ph = True
        def hide(_e=None):
            if getattr(entry, "_ph", False):
                entry.delete(0, "end")
                entry.configure(foreground=INK)
                entry._ph = False
        entry.bind("<FocusIn>", hide)
        entry.bind("<FocusOut>", show)
        entry._ph_text = text
        show()

    def _query(self) -> str:
        q = self.search.get().strip().lower()
        return "" if q == "search name or site name" else q

    # ---------- header chips ----------
    def _set_chips(self):
        for w in self.chips.winfo_children():
            w.destroy()
        n = len(self.clips)
        on_site = sum(1 for c in self.clips if c.status in ("skipped", "done"))
        new = sum(1 for c in self.clips if c.status == "waiting")
        failed = sum(1 for c in self.clips if c.status == "failed")
        for label, value, colour in (("recordings", n, INK), ("on the site", on_site, TEAL),
                                     ("new", new, AMBER), ("failed", failed, RED)):
            if label == "failed" and not failed:
                continue
            chip = tk.Frame(self.chips, bg=SURFACE, padx=14, pady=7)
            chip.pack(side="left", padx=(8, 0))
            tk.Label(chip, text=str(value), bg=SURFACE, fg=colour,
                     font=(FONT_SEMI, 13)).pack(side="left")
            tk.Label(chip, text=" " + label, bg=SURFACE, fg=INK_DIM,
                     font=(FONT, 9)).pack(side="left", pady=(3, 0))

    # ---------- recordings ----------
    def choose_src(self):
        d = filedialog.askdirectory(title="Folder with recordings + .stamp.json",
                                    initialdir=self.src_var.get() or ROOT)
        if d:
            self.src_var.set(d)
            self.scan()

    def choose_work(self):
        d = filedialog.askdirectory(title="Where encoded clips are written",
                                    initialdir=self.work_var.get() or ROOT)
        if d:
            self.work_var.set(d)
            self.scan()

    def scan(self):
        folder = Path(self.src_var.get())
        self.clips.clear()
        if not folder.exists():
            self.write_log(f"Folder not found: {folder}", "error")
            self._fill_recordings()
            return

        for stamp in sorted(folder.glob("*.stamp.json")):
            video = folder / (stamp.name[: -len(".stamp.json")] + ".mp4")
            if video.exists():
                self.clips.append(Clip.load(video, stamp))

        # Already on the site? The ledger knows, and so does the "site" block a
        # sync writes into the stamp - either is enough.
        ledger = load_ledger(self.work_var.get())
        for clip in self.clips:
            info = (crt_local.read_stamp(clip.stamp) or {}).get(crt_local.SITE_KEY) or {}
            clip.site_title = str(info.get("title") or "")
            if clip.status != "waiting":
                continue
            if clip.name in ledger:
                parts = ledger[clip.name].split("\t")
                clip.status = "skipped"
                clip.note = f"{parts[2]} #{parts[3]}" if len(parts) > 3 else "already published"
                if (not clip.site_title and len(parts) > 3
                        and str(parts[3]).strip().isdigit()):
                    clip.site_title = site_name(parts[2], int(parts[3]))
            elif info.get("video_id"):
                clip.status = "skipped"
                clip.note = f"{info.get('collection_id')} #{info.get('video_number')}"

        self._fill_recordings()
        new = sum(1 for c in self.clips if c.status == "waiting")
        self.write_log(f"Found {len(self.clips)} recording(s) in {folder} - "
                       f"{new} not yet on the site.")

    def _row_values(self, clip: Clip):
        return (clip.name, clip.site_title or "—", clip.frame_count or "—",
                clip.stamped_frame if clip.stamped_frame is not None else "—",
                STATUS_LABEL.get(clip.status, clip.status), clip.note)

    def _fill_recordings(self):
        want = self.filter.get()
        q = self._query()
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for clip in self.clips:
            label = STATUS_LABEL.get(clip.status, clip.status)
            if want == "New" and clip.status != "waiting":
                continue
            if want == "On site" and clip.status not in ("skipped", "done"):
                continue
            if want == "Failed" and clip.status != "failed":
                continue
            if q and q not in f"{clip.name} {clip.site_title} {label}".lower():
                continue
            tags = (clip.status,) + (("alt",) if shown % 2 else ())
            self.tree.insert("", "end", iid=clip.name, tags=tags, values=self._row_values(clip))
            shown += 1
        if shown:
            self.empty_lbl.place_forget()
        else:
            self.empty_lbl.configure(text="No recordings match." if self.clips else
                                     "No recordings with a .stamp.json found in the source folder.")
            self.empty_lbl.place(relx=.5, rely=.35, anchor="center")
        self._set_chips()

    def update_row(self, clip: Clip):
        if self.tree.exists(clip.name):
            alt = ("alt",) if "alt" in self.tree.item(clip.name, "tags") else ()
            self.tree.item(clip.name, tags=(clip.status,) + alt, values=self._row_values(clip))
        self._set_chips()

    def _selected_clip(self) -> Clip | None:
        sel = self.tree.selection()
        return next((c for c in self.clips if sel and c.name == sel[0]), None)

    def _encoded_of(self, clip: Clip) -> Path | None:
        p = Path(self.work_var.get()) / clip.name
        return p if p.exists() else None

    def _play(self, path: Path | None, what: str):
        if not path or not path.exists():
            messagebox.showinfo("Nothing to play", f"No {what} file found.", parent=self)
            return
        try:
            open_file(path)
            self.write_log(f"Playing {path.name} ({what}).")
        except Exception as exc:
            messagebox.showerror("Could not play it", str(exc), parent=self)

    def _play_selected(self):
        clip = self._selected_clip()
        if not clip:
            return
        enc = self._encoded_of(clip)
        if enc:
            self._play(enc, "encoded, as on the site")
        else:
            self._play(clip.video, "original recording")

    def _show_selected(self):
        clip = self._selected_clip()
        if clip:
            show_in_folder(clip.video)

    def _rec_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        clip = self._selected_clip()
        enc = self._encoded_of(clip)
        m = tk.Menu(self, tearoff=0, bg=SURFACE_2, fg=INK, activebackground=TEAL_DARK,
                    activeforeground=INK, bd=0, font=(FONT, 10))
        m.add_command(label="Play encoded (with flash, as on the site)",
                      command=lambda: self._play(enc, "encoded"),
                      state="normal" if enc else "disabled")
        m.add_command(label="Play original recording",
                      command=lambda: self._play(clip.video, "original recording"))
        m.add_separator()
        m.add_command(label="Show in folder", command=lambda: show_in_folder(clip.video))
        m.add_command(label="Open stamp file", command=lambda: open_file(clip.stamp))
        m.tk_popup(event.x_root, event.y_root)

    # ---------- run ----------
    def gather(self) -> Settings:
        s = self.settings
        s.src_dir, s.work_dir = self.src_var.get(), self.work_var.get()
        s.service_key = self.key_var.get().strip()
        try:
            s.per_collection = max(1, int(self.per_var.get() or 10))
        except (tk.TclError, ValueError):
            s.per_collection = 10
        s.add_flash, s.skip_existing = self.flash_var.get(), self.skip_var.get()
        s.save()
        return s

    def start(self):
        if self.pipeline and self.pipeline.is_alive():
            return
        if not self.clips:
            messagebox.showwarning("Nothing to do", "No clips with stamp data were found.")
            return
        s = self.gather()
        if not s.service_key:
            messagebox.showerror(
                "Service key required",
                "Paste your Supabase service_role key.\n\n"
                "Supabase dashboard -> Project Settings -> API -> service_role.\n\n"
                "The site's public key is read-only by design, so it cannot upload.")
            return
        if not have_ffmpeg():
            messagebox.showerror("ffmpeg missing", "Install ffmpeg and make sure it is on PATH.")
            return

        for clip in self.clips:                        # reset for a fresh run
            if clip.status in ("done", "failed", "skipped"):
                clip.status, clip.note = "waiting", ""
        self.filter.set("All")
        self._fill_recordings()

        self.go_btn.pack_forget()
        self.cancel_btn.pack(fill="x", side="bottom")
        self.progress.configure(value=0)
        self.pipeline = Pipeline(self.clips, s, self.events)
        self.pipeline.start()

    def stop(self):
        if self.pipeline:
            self.pipeline.cancel.set()
            self.write_log("Cancelling after the current clip...", "warn")

    def sync_local(self):
        if self.pipeline and self.pipeline.is_alive():
            messagebox.showinfo("Busy", "Wait for the current upload to finish.")
            return
        s = self.gather()
        if not s.service_key:
            messagebox.showwarning("Service key needed",
                                   "Paste the Supabase service_role key first.")
            return
        self.write_log("Syncing site names and folders ...")
        self.update_idletasks()
        run_sync(Supabase(s.service_key), s, self.write_log)
        self.scan()
        self.lib_tab.refresh(quiet=True)

    # ---------- log ----------
    def write_log(self, msg: str, tone: str = "info"):
        from datetime import datetime
        now = datetime.now()
        self.log.configure(state="normal")
        self.log.insert("end", now.strftime("%H:%M:%S  "), "time")
        self.log.insert("end", msg + "\n", tone)
        self.log.see("end")
        self.log.configure(state="disabled")
        try:
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(f"{now:%Y-%m-%d %H:%M:%S}  [{tone}]  {msg}\n")
        except Exception:
            pass

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.log.get("1.0", "end"))
        self.progress_lbl.configure(text="Log copied to the clipboard")

    # ---------- event pump ----------
    def _drain(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev["kind"]
                if kind == "log":
                    self.write_log(ev["msg"], ev.get("tone", "info"))
                elif kind == "row":
                    self.update_row(ev["clip"])
                elif kind == "progress":
                    self.progress.configure(value=ev["value"])
                    self.progress_lbl.configure(text=ev["text"])
                elif kind == "finished":
                    self.cancel_btn.pack_forget()
                    self.go_btn.pack(fill="x", side="bottom")
                    self.progress_lbl.configure(
                        text="Done" if ev.get("ok") else "Finished with errors")
                    self.lib_tab.loaded = False        # new clips on the site
                elif kind == "replaced":
                    self.scan()                        # the old recording may have moved
                    self.lib_tab.refresh(quiet=True)
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _tab_changed(self, _e=None):
        if self.tabs.select() == str(self.lib_tab) and not self.lib_tab.loaded:
            self.lib_tab.refresh()


# ===========================================================================
#  "On the site" tab - every published clip: play it, take it down, put it
#  back, or swap in a corrected file
# ===========================================================================
class LibraryPanel(ttk.Frame):
    def __init__(self, parent, app: "App"):
        super().__init__(parent, style="Card.TFrame")
        self.app = app
        self.rows: list[dict] = []
        self.files: dict[str, dict[str, Path]] = {}
        self.loaded = False
        self.query = tk.StringVar()
        self.show = tk.StringVar(value="All")
        self._build()

    # ---------- layout ----------
    def _build(self):
        bar = ttk.Frame(self, style="Card.TFrame", padding=(14, 12, 14, 10))
        bar.pack(fill="x")
        seg = ttk.Frame(bar, style="Card.TFrame")
        seg.pack(side="left")
        for name in ("All", "Live", "Taken down"):
            ttk.Radiobutton(seg, text=name, value=name, variable=self.show,
                            style="Seg.TRadiobutton", command=self._fill).pack(side="left", padx=(0, 2))
        ent = ttk.Entry(bar, textvariable=self.query, width=28)
        ent.pack(side="left", padx=(14, 0))
        self.app._placeholder(ent, self.query, "Find: CRT 1-07, 7, or file name")
        self.query.trace_add("write", lambda *_a: self._fill())
        self.count_lbl = ttk.Label(bar, text="", style="CardDim.TLabel")
        self.count_lbl.pack(side="left", padx=(14, 0))
        HoverButton(bar, "Refresh", self.refresh, "ghost").pack(side="right")

        ttk.Label(self, text="Double-click a clip to play it. Taking a clip down keeps "
                             "every mark made on it; deleting is only offered for clips "
                             "nobody has marked.",
                  style="CardDim.TLabel").pack(anchor="w", padx=14, pady=(0, 8))

        wrap = ttk.Frame(self, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=1)
        cols = ("site", "coll", "num", "recording", "marks", "state")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for cid, label, w, anchor in (
            ("site", "Site name", 110, "w"), ("coll", "Collection", 100, "w"),
            ("num", "#", 50, "center"), ("recording", "Recording", 300, "w"),
            ("marks", "Marks", 80, "center"), ("state", "State", 110, "w"),
        ):
            self.tree.heading(cid, text=label, anchor=anchor)
            self.tree.column(cid, width=w, anchor=anchor, stretch=(cid == "recording"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("live", foreground=INK)
        self.tree.tag_configure("retired", foreground="#6f8085")
        self.tree.tag_configure("alt", background=ROW_ALT)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_buttons())
        self.tree.bind("<Double-1>", lambda _e: self.play())
        self.tree.bind("<Return>", lambda _e: self.play())
        self.tree.bind("<Button-3>", self._menu)
        self.empty_lbl = ttk.Label(wrap, text="", style="CardDim.TLabel")

        acts = ttk.Frame(self, style="Card.TFrame", padding=(14, 10, 14, 12))
        acts.pack(fill="x")
        self.play_btn = HoverButton(acts, "Play", self.play, "primary")
        self.play_btn.pack(side="left")
        self.replace_btn = HoverButton(acts, "Replace file...", self.replace, "ghost")
        self.replace_btn.pack(side="left", padx=(8, 0))
        self.restore_btn = HoverButton(acts, "Put back", self.restore, "ghost")
        self.restore_btn.pack(side="left", padx=(8, 0))
        self.retire_btn = HoverButton(acts, "Take down", self.retire, "danger")
        self.retire_btn.pack(side="left", padx=(8, 0))
        self.delete_btn = HoverButton(acts, "Delete permanently", self.delete, "danger")
        self.delete_btn.pack(side="right")
        self._sync_buttons()

    # ---------- data ----------
    def _sb(self) -> Supabase:
        return Supabase(self.app.key_var.get().strip())

    def refresh(self, quiet: bool = False):
        if not self.app.key_var.get().strip():
            if not quiet:
                self._empty("Paste the Supabase service key (right) to see the clips on the site.")
            return
        self.count_lbl.configure(text="Loading...")
        self.update_idletasks()
        try:
            self.rows = self._sb().library()
        except Exception as exc:
            self.count_lbl.configure(text="")
            if not quiet:
                self.app.write_log(f"Could not load the site's clips: {exc}", "error")
                self._empty("Could not reach Supabase - see Activity below.")
            return
        self.files = local_files_by_clip(self.app.src_var.get(), self.app.work_var.get())
        self.loaded = True
        self._fill()

    def _empty(self, text):
        self.tree.delete(*self.tree.get_children())
        self.empty_lbl.configure(text=text)
        self.empty_lbl.place(relx=.5, rely=.35, anchor="center")

    def _recording(self, row) -> str:
        f = self.files.get(row.get("id"), {})
        p = f.get("source") or f.get("encoded")
        return p.name if p else (row.get("storage_path") or "")

    def _matches(self, row: dict, q: str) -> bool:
        if not q or q.startswith("find:"):
            return True
        num = str(row.get("video_number", ""))
        # A bare number means the CLIP number and nothing else - date-stamped
        # filenames contain digits everywhere.
        if q.isdigit():
            return q.lstrip("0") == num.lstrip("0")
        hay = " ".join(str(row.get(k) or "") for k in
                       ("video_number", "title", "storage_path", "collection_id")).lower()
        return q in hay + " " + self._recording(row).lower()

    def _fill(self):
        q = self.query.get().strip().lower()
        want = self.show.get()
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for row in self.rows:
            active = row.get("active") is not False
            if (want == "Live" and not active) or (want == "Taken down" and active):
                continue
            if not self._matches(row, q):
                continue
            marks = row.get("marks")
            tags = ("live" if active else "retired",) + (("alt",) if shown % 2 else ())
            self.tree.insert("", "end", iid=row["id"], tags=tags, values=(
                row.get("title") or "—", row.get("collection_id") or "—",
                row.get("video_number", "—"), self._recording(row),
                "?" if marks is None else marks,
                "Live" if active else "Taken down"))
            shown += 1
        live = sum(1 for r in self.rows if r.get("active") is not False)
        self.count_lbl.configure(
            text=f"{live} live · {len(self.rows) - live} taken down"
                 + ("" if not self.rows or self.rows[0].get("marks_known", True)
                    else "  (mark counts need supabase_retire_clips.sql)"))
        if shown:
            self.empty_lbl.place_forget()
        else:
            self.empty_lbl.configure(text="No clips match.")
            self.empty_lbl.place(relx=.5, rely=.35, anchor="center")
        self._sync_buttons()

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return next((r for r in self.rows if sel and r["id"] == sel[0]), None)

    def _sync_buttons(self):
        row = self._selected()
        active = bool(row) and row.get("active") is not False
        marks = row.get("marks") if row else None
        self.play_btn.configure(state="normal" if row else "disabled")
        self.retire_btn.configure(state="normal" if active else "disabled")
        self.restore_btn.configure(state="disabled" if (not row or active) else "normal")
        self.replace_btn.configure(state="normal" if row else "disabled")
        # Deleting cascades into the marks, so it is offered only when we KNOW
        # there are none. An unknown count (no migration) counts as "has marks".
        self.delete_btn.configure(state="normal" if (row and marks == 0) else "disabled")

    def _after_change(self):
        """Move the recording to match (taken_down/ or back), then redraw."""
        run_sync(self._sb(), self.app.gather(), self.app.write_log)
        self.app.scan()
        self.refresh()

    # ---------- play ----------
    def play(self, prefer: str = "local"):
        row = self._selected()
        if not row:
            return
        f = self.files.get(row["id"], {})
        local = f.get("encoded") or f.get("source")
        if prefer == "local" and local:
            self.app._play(local, f"{row.get('title')}, on this laptop")
        else:
            self.play_online(row)

    def play_online(self, row: dict):
        import urllib.parse
        import webbrowser
        url = PUBLIC_URL + urllib.parse.quote(row.get("storage_path") or "")
        webbrowser.open(url)
        self.app.write_log(f"Opened {row.get('title')} from the site in the browser.")

    def _menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        row = self._selected()
        f = self.files.get(row["id"], {})
        active = row.get("active") is not False
        m = tk.Menu(self, tearoff=0, bg=SURFACE_2, fg=INK, activebackground=TEAL_DARK,
                    activeforeground=INK, bd=0, font=(FONT, 10))
        m.add_command(label="Play encoded copy (as on the site)",
                      command=lambda: self.app._play(f.get("encoded"), "encoded"),
                      state="normal" if f.get("encoded") else "disabled")
        m.add_command(label="Play original recording",
                      command=lambda: self.app._play(f.get("source"), "original recording"),
                      state="normal" if f.get("source") else "disabled")
        m.add_command(label="Stream from the site (browser)",
                      command=lambda: self.play_online(row))
        m.add_separator()
        if active:
            m.add_command(label="Take down...", command=self.retire)
        else:
            m.add_command(label="Put back", command=self.restore)
        m.add_command(label="Replace file...", command=self.replace)
        m.tk_popup(event.x_root, event.y_root)

    # ---------- actions ----------
    def retire(self):
        row = self._selected()
        if not row:
            return
        marks = row.get("marks")
        extra = ("" if marks in (0, None) else
                 f"\n\nIts {marks} existing mark(s) are kept and stay analysable.")
        if not messagebox.askyesno(
                "Take this clip down?",
                f"{row.get('title')}  (clip #{row.get('video_number')})\n\n"
                f"It stops being served to participants straight away."
                f"{extra}\n\nYou can put it back at any time.",
                parent=self):
            return
        reason = simpledialog.askstring(
            "Why?", "A short note for the record (optional):", parent=self) or ""
        try:
            self._sb().set_active(row["id"], False, reason)
        except Exception as exc:
            messagebox.showerror("Could not take it down", str(exc), parent=self)
            return
        self.app.write_log(f"Took down {row.get('title')} ({row.get('storage_path')}).", "warn")
        self._after_change()

    def restore(self):
        row = self._selected()
        if not row:
            return
        try:
            self._sb().set_active(row["id"], True)
        except Exception as exc:
            messagebox.showerror("Could not put it back", str(exc), parent=self)
            return
        self.app.write_log(f"Put {row.get('title')} back on the site.", "good")
        self._after_change()

    def delete(self):
        row = self._selected()
        if not row or row.get("marks") != 0:
            return
        if not messagebox.askyesno(
                "Delete permanently?",
                f"{row.get('title')}  (clip #{row.get('video_number')})\n\n"
                "Nothing has marked this clip, so no study data is lost.\n"
                "The row and its file are removed for good. This cannot be undone.",
                parent=self):
            return
        try:
            sb = self._sb()
            sb.delete_video(row["id"])
            if row.get("storage_path"):
                sb.remove_file(row["storage_path"])
        except Exception as exc:
            messagebox.showerror("Could not delete", str(exc), parent=self)
            return
        self.app.write_log(f"Deleted {row.get('title')} ({row.get('storage_path')}) "
                           f"- it had no marks.", "warn")
        self.refresh()

    def replace(self):
        row = self._selected()
        if not row:
            return
        video = filedialog.askopenfilename(
            title=f"Corrected recording for {row.get('title')}",
            initialdir=self.app.src_var.get() or ROOT,
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi"), ("All files", "*.*")],
            parent=self)
        if not video:
            return
        vp = Path(video)
        stamp = vp.with_suffix("").with_suffix(".stamp.json")
        if not stamp.exists():
            stamp = vp.parent / (vp.stem + ".stamp.json")
        if not stamp.exists():
            if not messagebox.askyesno(
                    "No stamp file",
                    f"No {vp.stem}.stamp.json sits next to this recording.\n\n"
                    "Without it the clip has no release frame, so the site will "
                    "time from the start of the video instead.\n\nGo on anyway?",
                    parent=self):
                return
        if stamp.exists():
            clip = Clip.load(vp, stamp)
            if clip.status == "failed":
                messagebox.showerror("Could not read that recording", clip.note, parent=self)
                return
        else:
            # No sidecar: the clip still plays, it just has no release frame.
            clip = Clip(video=vp, stamp=vp)

        if not messagebox.askyesno(
                "Replace this clip?",
                f"{row.get('title')}  (clip #{row.get('video_number')})\n\n"
                f"New file: {vp.name}\n\n"
                "The version on the site now is taken down (its marks are kept), and "
                "this recording goes up in its place under the same name.",
                parent=self):
            return
        reason = simpledialog.askstring(
            "Why?", "A short note for the record (optional):", parent=self) or ""

        self.app.write_log(f"Replacing {row.get('title')} with {vp.name} ...")
        self.count_lbl.configure(text="Replacing - see Activity below")
        Replacer(row, clip, self.app.gather(), reason, self.app.events).start()


if __name__ == "__main__":
    App().mainloop()
