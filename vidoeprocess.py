import json
import re
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

SRC_DIR = Path(r"D:\project\Reserch\resorces\pump_recordings")
OUT_DIR = Path(r"D:\project\Reserch\resorces\editedvideos")

FLASH_FRAMES = 3
FLASH_COLOR = (0, 255, 0)  # BGR green
FLASH_ALPHA = 0.5

WINDOW_NAME = "Pump Recordings Preview"

# ---- layout ----
VIDEO_AREA_W = 960
VIDEO_AREA_H = 600
SCRUB_H = 28
BUTTON_BAR_H = 64
INFO_BAR_H = 34
WINDOW_W = VIDEO_AREA_W
WINDOW_H = VIDEO_AREA_H + SCRUB_H + BUTTON_BAR_H + INFO_BAR_H

# ---- colors (BGR) ----
COL_BG = (24, 24, 24)
COL_PANEL = (38, 38, 38)
COL_PANEL_BORDER = (60, 60, 60)
COL_TEXT = (235, 235, 235)
COL_TEXT_DIM = (150, 150, 150)
COL_ACCENT = (80, 200, 120)
COL_ACCENT_TEXT = (20, 20, 20)
COL_BTN = (55, 55, 55)
COL_BTN_HOVER = (75, 75, 75)
COL_BTN_ACTIVE = (95, 95, 95)
COL_SCRUB_BG = (55, 55, 55)
COL_SCRUB_FILL = (80, 200, 120)
COL_SCRUB_FLASH = (60, 60, 230)


def build_frames(video_path: Path, stamped_frame: int) -> tuple[list, float]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return [], 0

    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    flash_end = stamped_frame + FLASH_FRAMES - 1

    frames = []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if stamped_frame <= idx <= flash_end:
            overlay = frame.copy()
            overlay[:] = FLASH_COLOR
            frame = cv2.addWeighted(overlay, FLASH_ALPHA, frame, 1 - FLASH_ALPHA, 0)
        frames.append(frame)
        idx += 1

    cap.release()
    return frames, fps


def read_frames(video_path: Path) -> tuple[list, float]:
    """Reads frames as-is, without applying the flash overlay (for already-processed videos)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return [], 0

    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)

    cap.release()
    return frames, fps


def save_frames(frames: list, out_path: Path, fps: float) -> None:
    """Writes raw frames, then transcodes in place to browser-playable H.264 (+faststart)."""
    height, width = frames[0].shape[:2]
    raw_path = out_path.with_name(out_path.stem + ".raw.mp4")

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(raw_path), fourcc, fps, (width, height))
    for frame in frames:
        writer.write(frame)
    writer.release()

    transcode_for_web(raw_path, out_path)
    raw_path.unlink(missing_ok=True)


def transcode_for_web(src_path: Path, out_path: Path) -> None:
    """Re-encodes src_path to H.264/yuv420p with +faststart, writing to out_path."""
    tmp_path = out_path.with_name(out_path.stem + ".tmp.mp4")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src_path),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "20",
            "-movflags", "+faststart",
            "-an",
            str(tmp_path),
        ],
        check=True,
    )
    tmp_path.replace(out_path)


def is_web_ready(video_path: Path) -> bool:
    """Checks (via ffprobe) whether video_path is already H.264 - skip re-encoding if so."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "csv=p=0",
                str(video_path),
            ],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return result.stdout.strip() == "h264"


COPY_SUFFIX_RE = re.compile(r"^(?P<base>.+) \(\d+\)(?P<ext>\.[^.]+)$")


def clean_copies(directory: Path) -> None:
    """Deletes Windows-style duplicate files like 'name (1).mp4' when 'name.mp4' also exists."""
    for path in directory.iterdir():
        if not path.is_file():
            continue
        m = COPY_SUFFIX_RE.match(path.name)
        if not m:
            continue
        original = directory / f"{m.group('base')}{m.group('ext')}"
        if original.exists():
            path.unlink()
            print(f"deleted copy: {path.name}")


def process_all() -> list[dict]:
    """Builds + saves the flashed video for every stamp pair, returns clip metadata for browsing."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    clean_copies(OUT_DIR)
    clean_copies(SRC_DIR)
    clips = []

    for stamp_path in sorted(SRC_DIR.glob("*.stamp.json")):
        video_name = stamp_path.name[: -len(".stamp.json")] + ".mp4"
        video_path = SRC_DIR / video_name
        if not video_path.exists():
            print(f"skip (missing video): {video_name}")
            continue

        with open(stamp_path, "r", encoding="utf-8") as f:
            stamp = json.load(f)
        stamped_frame = stamp.get("stamped_frame")
        if stamped_frame is None:
            print(f"skip (no stamped_frame): {video_name}")
            continue

        out_path = OUT_DIR / video_name
        if out_path.exists():
            if not is_web_ready(out_path):
                print(f"re-encoding for web: {video_name}")
                transcode_for_web(out_path, out_path)

            frames, fps = read_frames(out_path)
            if not frames:
                print(f"skip (cannot open/read existing): {video_name}")
                continue
            print(f"already exists, skipped save: {video_name} ({len(frames)} frames)")
        else:
            frames, fps = build_frames(video_path, stamped_frame)
            if not frames:
                print(f"skip (cannot open/read): {video_name}")
                continue
            save_frames(frames, out_path, fps)
            print(f"saved: {video_name} ({len(frames)} frames, flash {stamped_frame}-{stamped_frame + FLASH_FRAMES - 1})")

        shutil.copy2(stamp_path, OUT_DIR / stamp_path.name)

        clips.append({
            "name": video_name,
            "frames": frames,
            "fps": fps,
            "flash_start": stamped_frame,
            "flash_end": stamped_frame + FLASH_FRAMES - 1,
        })

    return clips


class Button:
    def __init__(self, x, y, w, h, label, key_hint=""):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.label = label
        self.key_hint = key_hint
        self.hover = False
        self.pressed_until = 0

    def contains(self, px, py):
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    def draw(self, canvas, active=False, frame_tick=0):
        color = COL_BTN_ACTIVE if (active or frame_tick < self.pressed_until) else (COL_BTN_HOVER if self.hover else COL_BTN)
        cv2.rectangle(canvas, (self.x, self.y), (self.x + self.w, self.y + self.h), color, -1)
        cv2.rectangle(canvas, (self.x, self.y), (self.x + self.w, self.y + self.h), COL_PANEL_BORDER, 1)

        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.55
        thickness = 1
        (tw, th), _ = cv2.getTextSize(self.label, font, scale, thickness)
        tx = self.x + (self.w - tw) // 2
        ty = self.y + (self.h + th) // 2
        cv2.putText(canvas, self.label, (tx, ty), font, scale, COL_TEXT, thickness, cv2.LINE_AA)

        if self.key_hint:
            font2 = cv2.FONT_HERSHEY_SIMPLEX
            (kw, _), _ = cv2.getTextSize(self.key_hint, font2, 0.38, 1)
            kx = self.x + (self.w - kw) // 2
            cv2.putText(canvas, self.key_hint, (kx, self.y + self.h - 6), font2, 0.38, COL_TEXT_DIM, 1, cv2.LINE_AA)


class Player:
    def __init__(self, clips):
        self.clips = clips
        self.clip_idx = 0
        self.frame_idx = 0
        self.playing = True
        self.quit = False
        self.frame_tick = 0

        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(WINDOW_NAME, self.on_mouse)

        bw, gap = 96, 8
        by = VIDEO_AREA_H + SCRUB_H + 8
        bh = BUTTON_BAR_H - 16
        x = 12
        self.btn_prev = Button(x, by, bw, bh, "Prev", "P / <-"); x += bw + gap
        self.btn_play = Button(x, by, bw, bh, "Play", "Space"); x += bw + gap
        self.btn_next = Button(x, by, bw, bh, "Next", "N / ->"); x += bw + gap
        self.btn_step_back = Button(x, by, bw, bh, "-1 Frame", "A"); x += bw + gap
        self.btn_step_fwd = Button(x, by, bw, bh, "+1 Frame", "D"); x += bw + gap
        self.btn_quit = Button(WINDOW_W - 12 - bw, by, bw, bh, "Quit", "Q / Esc")

        self.buttons = [self.btn_prev, self.btn_play, self.btn_next,
                         self.btn_step_back, self.btn_step_fwd, self.btn_quit]

        self.scrub_y = VIDEO_AREA_H
        self.scrub_x0 = 12
        self.scrub_x1 = WINDOW_W - 12
        self.dragging_scrub = False

    @property
    def clip(self):
        return self.clips[self.clip_idx]

    def goto_clip(self, idx):
        self.clip_idx = max(0, min(idx, len(self.clips) - 1))
        self.frame_idx = 0
        self.playing = True

    def step(self, delta):
        n = len(self.clip["frames"])
        self.frame_idx = max(0, min(self.frame_idx + delta, n - 1))
        self.playing = False

    def toggle_play(self):
        self.playing = not self.playing

    def scrub_to_x(self, px):
        n = len(self.clip["frames"])
        ratio = (px - self.scrub_x0) / max(1, (self.scrub_x1 - self.scrub_x0))
        ratio = max(0.0, min(1.0, ratio))
        self.frame_idx = min(int(ratio * n), n - 1)
        self.playing = False

    def on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_MOUSEMOVE:
            for b in self.buttons:
                b.hover = b.contains(x, y)
            if self.dragging_scrub:
                self.scrub_to_x(x)

        elif event == cv2.EVENT_LBUTTONDOWN:
            if self.scrub_y <= y <= self.scrub_y + SCRUB_H:
                self.dragging_scrub = True
                self.scrub_to_x(x)
                return
            if self.btn_prev.contains(x, y):
                self.btn_prev.pressed_until = self.frame_tick + 5
                self.goto_clip(self.clip_idx - 1)
            elif self.btn_play.contains(x, y):
                self.btn_play.pressed_until = self.frame_tick + 5
                self.toggle_play()
            elif self.btn_next.contains(x, y):
                self.btn_next.pressed_until = self.frame_tick + 5
                self.goto_clip(self.clip_idx + 1)
            elif self.btn_step_back.contains(x, y):
                self.btn_step_back.pressed_until = self.frame_tick + 5
                self.step(-1)
            elif self.btn_step_fwd.contains(x, y):
                self.btn_step_fwd.pressed_until = self.frame_tick + 5
                self.step(1)
            elif self.btn_quit.contains(x, y):
                self.quit = True

        elif event == cv2.EVENT_LBUTTONUP:
            self.dragging_scrub = False

    def render(self):
        canvas = np.full((WINDOW_H, WINDOW_W, 3), COL_BG, dtype=np.uint8)
        clip = self.clip
        frame = clip["frames"][self.frame_idx]

        fh, fw = frame.shape[:2]
        scale = min(VIDEO_AREA_W / fw, VIDEO_AREA_H / fh)
        nw, nh = int(fw * scale), int(fh * scale)
        resized = cv2.resize(frame, (nw, nh))
        ox, oy = (VIDEO_AREA_W - nw) // 2, (VIDEO_AREA_H - nh) // 2
        canvas[oy:oy + nh, ox:ox + nw] = resized

        is_flash = clip["flash_start"] <= self.frame_idx <= clip["flash_end"]
        if is_flash:
            cv2.rectangle(canvas, (0, 0), (VIDEO_AREA_W - 1, VIDEO_AREA_H - 1), (0, 255, 0), 4)

        n_frames = len(clip["frames"])

        # scrub bar
        sy0, sy1 = self.scrub_y + 8, self.scrub_y + SCRUB_H - 8
        cv2.rectangle(canvas, (self.scrub_x0, sy0), (self.scrub_x1, sy1), COL_SCRUB_BG, -1)
        track_w = self.scrub_x1 - self.scrub_x0
        flash_x0 = self.scrub_x0 + int(track_w * clip["flash_start"] / max(1, n_frames - 1))
        flash_x1 = self.scrub_x0 + int(track_w * clip["flash_end"] / max(1, n_frames - 1))
        cv2.rectangle(canvas, (flash_x0, sy0), (flash_x1, sy1), COL_SCRUB_FLASH, -1)
        progress_x = self.scrub_x0 + int(track_w * self.frame_idx / max(1, n_frames - 1))
        cv2.rectangle(canvas, (self.scrub_x0, sy0), (progress_x, sy1), COL_SCRUB_FILL, 2)
        cv2.circle(canvas, (progress_x, (sy0 + sy1) // 2), 6, COL_SCRUB_FILL, -1)

        # button bar background
        bar_y0 = VIDEO_AREA_H + SCRUB_H
        cv2.rectangle(canvas, (0, bar_y0), (WINDOW_W, bar_y0 + BUTTON_BAR_H), COL_PANEL, -1)
        cv2.line(canvas, (0, bar_y0), (WINDOW_W, bar_y0), COL_PANEL_BORDER, 1)

        self.btn_play.label = "Pause" if self.playing else "Play"
        for b in self.buttons:
            b.draw(canvas, frame_tick=self.frame_tick)

        # info bar (bottom)
        info_y0 = bar_y0 + BUTTON_BAR_H
        cv2.rectangle(canvas, (0, info_y0), (WINDOW_W, WINDOW_H), COL_PANEL, -1)
        cv2.line(canvas, (0, info_y0), (WINDOW_W, info_y0), COL_PANEL_BORDER, 1)

        left_text = f"{self.clip_idx + 1}/{len(self.clips)}  {clip['name']}"
        right_text = f"frame {self.frame_idx + 1}/{n_frames}  |  {clip['fps']:.1f} fps"
        cv2.putText(canvas, left_text, (12, info_y0 + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, COL_TEXT, 1, cv2.LINE_AA)
        (rw, _), _ = cv2.getTextSize(right_text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.putText(canvas, right_text, (WINDOW_W - 12 - rw, info_y0 + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    COL_ACCENT if is_flash else COL_TEXT_DIM, 1, cv2.LINE_AA)

        return canvas

    def run(self):
        while not self.quit:
            self.frame_tick += 1
            canvas = self.render()
            cv2.imshow(WINDOW_NAME, canvas)

            delay_ms = max(1, int(1000 / self.clip["fps"])) if self.playing else 30
            key = cv2.waitKey(delay_ms) & 0xFF
            if key == 255:
                key = -1

            if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                break

            if key in (ord("q"), 27):
                break
            elif key == ord(" "):
                self.toggle_play()
            elif key == ord("n"):
                self.goto_clip(self.clip_idx + 1)
            elif key == ord("p"):
                self.goto_clip(self.clip_idx - 1)
            elif key in (ord("d"), 83):
                self.step(1)
            elif key in (ord("a"), 81):
                self.step(-1)
            elif self.playing:
                n = len(self.clip["frames"])
                self.frame_idx += 1
                if self.frame_idx >= n:
                    self.frame_idx = n - 1
                    self.playing = False

        cv2.destroyAllWindows()


def browse(clips: list[dict]) -> None:
    if not clips:
        print("no clips to preview.")
        return
    Player(clips).run()


def main() -> None:
    clips = process_all()
    browse(clips)


if __name__ == "__main__":
    main()
