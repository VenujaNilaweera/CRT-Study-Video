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
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import requests

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
    "waiting":   INK_DIM,
    "skipped":   INK_DIM,
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

    def insert_video(self, row: dict) -> None:
        r = requests.post(
            f"{self.rest}/videos",
            headers={**self.headers, "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=row, timeout=60,
        )
        if not r.ok:
            raise RuntimeError(f"row insert failed ({r.status_code}): {r.text[:200]}")


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
                real_fps = (clip.frame_count / clip.recording_duration_s
                            if clip.frame_count and clip.recording_duration_s else clip.capture_fps)
                sb.insert_video({
                    "collection_id": cur_id,
                    "video_number": number,
                    "title": f"CRT Test {number:02d}",
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

                clip.collection_id, clip.video_number = cur_id, number
                self.set_status(clip, "done", f"{cur_id} #{number}")
                self.log(f"  {clip.name} -> {cur_id} #{number} ({frames} frames)", "good")
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
            self.log("Reload the study site to see them (hard-refresh).", "good")
        self.emit("finished", ok=failed == 0)


# ===========================================================================
#  GUI
# ===========================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CRT Study Studio")
        self.configure(bg=BG)
        self.geometry("1060x760")
        self.minsize(900, 620)

        self.settings = Settings.load()
        self.clips: list[Clip] = []
        self.events: queue.Queue = queue.Queue()
        self.pipeline: Pipeline | None = None

        self._build_style()
        self._build_ui()
        self.after(80, self._drain)
        if Path(self.settings.src_dir).exists():
            self.scan()

    # ---------- theme ----------
    def _build_style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=INK,
                     fieldbackground=SURFACE_2, borderwidth=0)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=SURFACE)
        st.configure("TLabel", background=BG, foreground=INK, font=("Segoe UI", 10))
        st.configure("Card.TLabel", background=SURFACE, foreground=INK)
        st.configure("Dim.TLabel", background=BG, foreground=INK_DIM, font=("Segoe UI", 9))
        st.configure("CardDim.TLabel", background=SURFACE, foreground=INK_DIM,
                     font=("Segoe UI", 9))
        st.configure("H1.TLabel", background=BG, foreground=INK,
                     font=("Segoe UI Semibold", 15))
        st.configure("H2.TLabel", background=SURFACE, foreground=INK,
                     font=("Segoe UI Semibold", 10))
        st.configure("TEntry", fieldbackground=SURFACE_2, foreground=INK,
                     insertcolor=INK, bordercolor=LINE, lightcolor=LINE,
                     darkcolor=LINE, padding=6)
        st.configure("TCheckbutton", background=SURFACE, foreground=INK_DIM)
        st.map("TCheckbutton", background=[("active", SURFACE)])
        st.configure("TSpinbox", fieldbackground=SURFACE_2, foreground=INK,
                     arrowcolor=INK, bordercolor=LINE, padding=4)
        st.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                     foreground=INK, rowheight=26, borderwidth=0)
        st.configure("Treeview.Heading", background=SURFACE_2, foreground=INK_DIM,
                     relief="flat", font=("Segoe UI", 9))
        st.map("Treeview", background=[("selected", SURFACE_2)])
        st.configure("TProgressbar", background=TEAL, troughcolor=SURFACE_2,
                     borderwidth=0, thickness=8)

    def _btn(self, parent, text, cmd, kind="primary"):
        bg = {"primary": TEAL, "ghost": SURFACE_2, "danger": RED}[kind]
        fg = "#08201c" if kind == "primary" else INK
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=TEAL_DARK if kind == "primary" else LINE,
                      activeforeground=fg, relief="flat", bd=0,
                      font=("Segoe UI Semibold", 10), padx=16, pady=9, cursor="hand2")
        return b

    # ---------- layout ----------
    def _build_ui(self):
        head = ttk.Frame(self, padding=(18, 14, 18, 8))
        head.pack(fill="x")
        ttk.Label(head, text="CRT Study Studio", style="H1.TLabel").pack(side="left")
        ttk.Label(head, text="  process recordings, then publish them to the study site",
                  style="Dim.TLabel").pack(side="left", padx=(8, 0))
        self.badge = ttk.Label(head, text="", style="Dim.TLabel")
        self.badge.pack(side="right")

        body = ttk.Frame(self, padding=(18, 0, 18, 8))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=310)
        body.rowconfigure(0, weight=1)

        # ---- clip table ----
        left = ttk.Frame(body, style="Card.TFrame", padding=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        cols = ("name", "frames", "release", "status", "note")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="none")
        for cid, label, w, anchor in (
            ("name", "Clip", 250, "w"), ("frames", "Frames", 70, "center"),
            ("release", "Release", 70, "center"), ("status", "Status", 90, "w"),
            ("note", "Detail", 220, "w"),
        ):
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=w, anchor=anchor,
                             stretch=(cid in ("name", "note")))
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for status, colour in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=colour)

        # ---- settings ----
        side = ttk.Frame(body, style="Card.TFrame", padding=16)
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)

        def section(text, pady=(0, 6)):
            ttk.Label(side, text=text, style="H2.TLabel").pack(anchor="w", pady=pady)

        section("Source folder")
        self.src_var = tk.StringVar(value=self.settings.src_dir)
        ttk.Entry(side, textvariable=self.src_var).pack(fill="x")
        row = ttk.Frame(side, style="Card.TFrame")
        row.pack(fill="x", pady=(6, 14))
        self._btn(row, "Choose...", self.choose_src, "ghost").pack(side="left")
        self._btn(row, "Rescan", self.scan, "ghost").pack(side="left", padx=6)

        section("Working folder (encoded output)")
        self.work_var = tk.StringVar(value=self.settings.work_dir)
        ttk.Entry(side, textvariable=self.work_var).pack(fill="x")
        self._btn(side, "Choose...", self.choose_work, "ghost").pack(anchor="w", pady=(6, 14))

        section("Supabase service_role key")
        self.key_var = tk.StringVar(value=self.settings.service_key)
        ttk.Entry(side, textvariable=self.key_var, show="*").pack(fill="x")
        ttk.Label(side, text="Saved locally in studio_config.json (git-ignored).\n"
                             "Never put this key in index.html.",
                  style="CardDim.TLabel", wraplength=270,
                  justify="left").pack(anchor="w", pady=(5, 14))

        section("Clips per collection")
        self.per_var = tk.IntVar(value=self.settings.per_collection)
        ttk.Spinbox(side, from_=1, to=100, textvariable=self.per_var,
                    width=6).pack(anchor="w", pady=(0, 14))

        self.flash_var = tk.BooleanVar(value=self.settings.add_flash)
        ttk.Checkbutton(side, text="Add green release flash",
                        variable=self.flash_var).pack(anchor="w")
        self.skip_var = tk.BooleanVar(value=self.settings.skip_existing)
        ttk.Checkbutton(side, text="Skip clips already on the site",
                        variable=self.skip_var).pack(anchor="w", pady=(0, 16))

        self.go_btn = self._btn(side, "Process & Upload", self.start)
        self.go_btn.pack(fill="x")
        self.cancel_btn = self._btn(side, "Cancel", self.stop, "danger")

        # ---- progress + log ----
        foot = ttk.Frame(self, padding=(18, 0, 18, 16))
        foot.pack(fill="x")
        self.progress = ttk.Progressbar(foot, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        self.progress_lbl = ttk.Label(foot, text="Idle", style="Dim.TLabel")
        self.progress_lbl.pack(anchor="w", pady=(5, 8))

        self.log = tk.Text(foot, height=9, bg=SURFACE, fg=INK_DIM, relief="flat",
                           font=("Consolas", 9), wrap="word", padx=12, pady=10,
                           insertbackground=INK)
        self.log.pack(fill="x")
        for tone, colour in (("info", INK_DIM), ("good", GREEN),
                             ("warn", AMBER), ("error", RED)):
            self.log.tag_configure(tone, foreground=colour)
        self.log.configure(state="disabled")

        if not have_ffmpeg():
            self.write_log("ffmpeg/ffprobe not found on PATH - encoding will fail.", "error")
        if not HAS_CV2:
            self.write_log("opencv-python not installed - the green flash is disabled.", "warn")

    # ---------- actions ----------
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

    def scan(self):
        folder = Path(self.src_var.get())
        self.tree.delete(*self.tree.get_children())
        self.clips.clear()
        if not folder.exists():
            self.write_log(f"Folder not found: {folder}", "error")
            self.badge.configure(text="")
            return

        for stamp in sorted(folder.glob("*.stamp.json")):
            video = folder / (stamp.name[: -len(".stamp.json")] + ".mp4")
            if video.exists():
                self.clips.append(Clip.load(video, stamp))

        # Mark what the ledger already knows about, so the table shows at a
        # glance which clips are new before anything is run.
        ledger = load_ledger(self.work_var.get())
        for clip in self.clips:
            if clip.status == "waiting" and clip.name in ledger:
                parts = ledger[clip.name].split("\t")
                where = f"{parts[2]} #{parts[3]}" if len(parts) > 3 else "already published"
                clip.status, clip.note = "skipped", where

        for clip in self.clips:
            self.tree.insert("", "end", iid=clip.name, tags=(clip.status,), values=(
                clip.name,
                clip.frame_count or "-",
                clip.stamped_frame if clip.stamped_frame is not None else "-",
                clip.status, clip.note,
            ))
        n = len(self.clips)
        new = sum(1 for c in self.clips if c.status == "waiting")
        self.badge.configure(text=f"{n} clip{'s' if n != 1 else ''}  ·  {new} new")
        self.write_log(f"Found {n} clip(s) in {folder} - {new} not yet published.")

    def gather(self) -> Settings:
        s = self.settings
        s.src_dir, s.work_dir = self.src_var.get(), self.work_var.get()
        s.service_key = self.key_var.get().strip()
        s.per_collection = max(1, int(self.per_var.get() or 10))
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
                self.update_row(clip)

        self.go_btn.pack_forget()
        self.cancel_btn.pack(fill="x")
        self.progress.configure(value=0)
        self.pipeline = Pipeline(self.clips, s, self.events)
        self.pipeline.start()

    def stop(self):
        if self.pipeline:
            self.pipeline.cancel.set()
            self.write_log("Cancelling after the current clip...", "warn")

    # ---------- event pump ----------
    def update_row(self, clip: Clip):
        if self.tree.exists(clip.name):
            self.tree.item(clip.name, tags=(clip.status,), values=(
                clip.name,
                clip.frame_count or "-",
                clip.stamped_frame if clip.stamped_frame is not None else "-",
                clip.status, clip.note,
            ))

    def write_log(self, msg: str, tone: str = "info"):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", tone)
        self.log.see("end")
        self.log.configure(state="disabled")

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
                    self.go_btn.pack(fill="x")
                    self.progress_lbl.configure(
                        text="Done" if ev.get("ok") else "Finished with errors")
        except queue.Empty:
            pass
        self.after(80, self._drain)


if __name__ == "__main__":
    App().mainloop()
