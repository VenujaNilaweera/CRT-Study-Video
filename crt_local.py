"""
crt_local.py - keeps the recordings on this laptop in step with the study site.

Three jobs, all driven by the site's own clip list (Supabase `videos`):

1. SITE NAME IN THE STAMP FILE
   A recording is called something like 2026-08-16_14-02-11.mp4 here, but
   participants, the analysis and every report call it "CRT 1-07". After a
   clip is published its <name>.stamp.json gains a "site" block:

       "site": {"title": "CRT 1-07", "video_id": "<uuid>", "collection_id": "col1",
                "video_number": 7, "storage_path": "2026-08-16_14-02-11.mp4",
                "active": true, "synced_at": "..."}

   Nothing else in the file is touched, and the recording itself is never
   renamed. The site name comes FROM the site, so it can never drift from what
   participants saw.

2. TAKEN-DOWN CLIPS OUT OF THE WAY
   A clip retired on the site has its recording (and stamp) moved into a
   "taken_down" subfolder of the folder it was in, so it is never mixed up with
   the good ones or re-uploaded. Putting a clip back moves it back.

3. A NAME SHEET
   clip_names.csv in the working folder: one line per clip on the site, with
   the site name next to the local recording name. Opens in Excel.

A file is only ever moved or labelled when it is CERTAIN which clip it is:
either its stamp already names the clip's uuid, or exactly one clip on the site
uses its filename. Anything ambiguous is left exactly where it is and reported.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SITE_KEY = "site"
TAKEN_DOWN_DIR = "taken_down"
NAMES_SHEET = "clip_names.csv"
STAMP_SUFFIX = ".stamp.json"
_REPLACEMENT_RE = re.compile(r"^(?P<stem>.+)__r\d+(?P<ext>\.[^.]+)$")


@dataclass
class LocalClip:
    stamp: Path
    video: Path                 # may not exist (a stamp copied without its clip)
    home: Path                  # the folder it belongs in (never the taken_down one)

    @property
    def name(self) -> str:
        return self.video.name

    @property
    def taken_down(self) -> bool:
        return self.stamp.parent.name == TAKEN_DOWN_DIR


def stamp_path(video: Path) -> Path:
    return video.parent / (video.stem + STAMP_SUFFIX)


def video_for(stamp: Path) -> Path:
    return stamp.parent / (stamp.name[: -len(STAMP_SUFFIX)] + ".mp4")


def read_stamp(stamp: Path) -> dict | None:
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def site_block(row: dict) -> dict:
    return {
        "title": row.get("title"),
        "video_id": row.get("id"),
        "collection_id": row.get("collection_id"),
        "video_number": row.get("video_number"),
        "storage_path": row.get("storage_path"),
        "active": row.get("active") is not False,
    }


def write_site_info(stamp: Path, row: dict) -> bool:
    """Record which site clip this recording is. True if the file changed."""
    if stamp.suffix != ".json" or not stamp.exists() or not row.get("id"):
        return False
    data = read_stamp(stamp)
    if data is None:
        return False
    new = site_block(row)
    old = {k: v for k, v in (data.get(SITE_KEY) or {}).items() if k != "synced_at"}
    if old == new:
        return False
    data[SITE_KEY] = {**new, "synced_at": datetime.now().isoformat(timespec="seconds")}
    _write_json_atomic(stamp, data)
    return True


def site_title_of(stamp: Path) -> str:
    data = read_stamp(stamp) or {}
    return str((data.get(SITE_KEY) or {}).get("title") or "")


def local_clips(*folders: str | Path) -> list[LocalClip]:
    """Every <name>.stamp.json in the folders and their taken_down subfolders."""
    out: list[LocalClip] = []
    seen: set[Path] = set()
    for folder in folders:
        if not folder:
            continue
        home = Path(folder)
        for where in (home, home / TAKEN_DOWN_DIR):
            if not where.is_dir():
                continue
            for stamp in sorted(where.glob("*" + STAMP_SUFFIX)):
                key = stamp.resolve()
                if key in seen:
                    continue
                seen.add(key)
                out.append(LocalClip(stamp=stamp, video=video_for(stamp), home=home))
    return out


def match_row(clip: LocalClip, rows: list[dict]) -> tuple[dict | None, str]:
    """Which site clip a local recording is, or (None, why not)."""
    by_id = {r.get("id"): r for r in rows if r.get("id")}
    info = (read_stamp(clip.stamp) or {}).get(SITE_KEY) or {}
    if info.get("video_id") in by_id:
        return by_id[info["video_id"]], ""
    if info.get("video_id"):
        return None, "not on the site"      # deleted there (only ever unmarked clips)

    exact = [r for r in rows if r.get("storage_path") == clip.name]
    if not exact:
        return None, "not on the site"
    if len(exact) > 1:
        return None, "more than one site clip uses this filename"
    # A corrected re-upload goes up as <stem>__r2.mp4 while the local file may
    # still carry the original name. If the clip under the original name is
    # retired and such a replacement exists, this file could be either the bad
    # recording or the corrected one - do not guess.
    row = exact[0]
    if row.get("active") is False:
        stem, ext = clip.video.stem, clip.video.suffix
        for r in rows:
            m = _REPLACEMENT_RE.match(str(r.get("storage_path") or ""))
            if m and m.group("stem") == stem and m.group("ext") == ext and r is not row:
                return None, ("the clip with this name was taken down and replaced - "
                              "cannot tell whether this file is the old or the new one")
    return row, ""


def _move_pair(clip: LocalClip, dest_dir: Path) -> str:
    """Move the recording + its stamp into dest_dir. Returns '' or a problem."""
    items = [p for p in (clip.video, clip.stamp) if p.exists()]
    clash = [p for p in items if (dest_dir / p.name).exists()]
    if clash:
        return f"{clash[0].name} already exists in {dest_dir}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for p in items:
        shutil.move(str(p), str(dest_dir / p.name))
    return ""


@dataclass
class SyncReport:
    labelled: int = 0
    moved_out: list[str] = None
    moved_back: list[str] = None
    unmatched: list[str] = None
    problems: list[str] = None
    sheet: Path | None = None

    def __post_init__(self):
        self.moved_out = self.moved_out or []
        self.moved_back = self.moved_back or []
        self.unmatched = self.unmatched or []
        self.problems = self.problems or []

    def lines(self) -> list[tuple[str, str]]:
        """(message, tone) pairs for a log."""
        out = [(f"Site names written into {self.labelled} stamp file(s).", "good")]
        for n in self.moved_out:
            out.append((f"  moved to {TAKEN_DOWN_DIR}/: {n}", "warn"))
        for n in self.moved_back:
            out.append((f"  moved back (clip is live again): {n}", "good"))
        for n in self.problems:
            out.append((f"  left alone: {n}", "error"))
        if self.unmatched:
            out.append((f"{len(self.unmatched)} local recording(s) are not on the site "
                        f"(not uploaded yet) - left as they are.", "info"))
        if self.sheet:
            out.append((f"Name sheet: {self.sheet}", "good"))
        return out


def sync_local(rows: list[dict], src_dir: str | Path, work_dir: str | Path,
               move: bool = True) -> SyncReport:
    """Label every local recording with its site name, file taken-down clips
    into taken_down/ (and live ones back out), and write the name sheet."""
    rep = SyncReport()
    matched: dict[str, list[LocalClip]] = {}
    for clip in local_clips(src_dir, work_dir):
        row, why = match_row(clip, rows)
        if row is None:
            if why == "not on the site":
                rep.unmatched.append(clip.name)
            else:
                rep.problems.append(f"{clip.stamp.parent / clip.name} - {why}")
            continue
        if write_site_info(clip.stamp, row):
            rep.labelled += 1
        matched.setdefault(row["id"], []).append(clip)
        if not move:
            continue
        retired = row.get("active") is False
        if retired and not clip.taken_down:
            problem = _move_pair(clip, clip.home / TAKEN_DOWN_DIR)
            (rep.problems.append(f"{clip.name} - {problem}") if problem
             else rep.moved_out.append(f"{clip.home.name}/{clip.name}  ({row.get('title')})"))
        elif not retired and clip.taken_down:
            problem = _move_pair(clip, clip.home)
            (rep.problems.append(f"{clip.name} - {problem}") if problem
             else rep.moved_back.append(f"{clip.home.name}/{clip.name}  ({row.get('title')})"))

    if work_dir:
        try:
            rep.sheet = write_names_sheet(Path(work_dir), rows, matched)
        except PermissionError:
            rep.problems.append(f"{NAMES_SHEET} is open in Excel - close it and sync again")
    return rep


def write_names_sheet(work_dir: Path, rows: list[dict],
                      matched: dict[str, list[LocalClip]]) -> Path:
    path = work_dir / NAMES_SHEET
    work_dir.mkdir(parents=True, exist_ok=True)

    def order(r):
        digits = "".join(ch for ch in str(r.get("collection_id") or "") if ch.isdigit())
        return (int(digits) if digits else 0, str(r.get("collection_id") or ""),
                r.get("video_number") or 0, r.get("active") is False)

    with path.open("w", newline="", encoding="utf-8-sig") as fh:   # -sig: Excel reads UTF-8
        w = csv.writer(fh)
        w.writerow(["site_name", "state", "collection", "clip_number", "recording_file",
                    "local_folder", "marks", "storage_path", "video_id"])
        for r in sorted(rows, key=order):
            locals_ = matched.get(r.get("id"), [])
            names = sorted({c.name for c in locals_})
            folders = sorted({str(c.stamp.parent) for c in locals_})
            marks = r.get("marks")
            w.writerow([
                r.get("title") or "", "taken down" if r.get("active") is False else "live",
                r.get("collection_id") or "", r.get("video_number") or "",
                "; ".join(names) or "(not found on this laptop)", "; ".join(folders),
                "" if marks is None else marks, r.get("storage_path") or "", r.get("id") or "",
            ])
    return path


def recording_names(*folders: str | Path) -> dict[str, str]:
    """video_id -> local recording filename, for the analysis.

    Uses only what the stamps already record (the "site" block written by a
    sync), so it needs no network and never guesses."""
    out: dict[str, str] = {}
    for clip in local_clips(*folders):
        info = (read_stamp(clip.stamp) or {}).get(SITE_KEY) or {}
        vid = info.get("video_id")
        if vid and (vid not in out or not clip.taken_down):
            out[vid] = clip.name
    return out
