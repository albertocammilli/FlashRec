"""
FlashRec - a minimal screen recorder with a time lapse mode.

Modes
  Record      1:1 real time capture, optionally with microphone and/or system
              audio. Audio tracks are muxed into the file when you stop.
  Time lapse  frames are sampled every (multiplier / fps) seconds, so a 200x
              lapse grabs one frame every 6.7 seconds. Frames that are never
              captured are never encoded, which is why this stays light.

Video is encoded while recording, never afterwards. ffmpeg with libx264 is used
when available, OpenCV's mp4v writer otherwise.

On Windows the app window and the timer overlay are marked with
WDA_EXCLUDEFROMCAPTURE, so neither of them shows up in the recording.
"""

import ctypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass

import cv2
import mss
import numpy as np
from PyQt6.QtCore import (QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt,
                          QThread, QTimer, pyqtProperty, pyqtSignal)
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QApplication, QComboBox, QGraphicsOpacityEffect,
                             QHBoxLayout, QLabel, QVBoxLayout, QWidget)

try:
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except Exception:                                                   # noqa: BLE001
    sd = None
    AUDIO_AVAILABLE = False

APP_NAME = "FlashRec"
SPEEDS = [5, 10, 30, 60, 100, 200]
OUTPUT_FPS = 30
SAMPLE_RATE = 48000
COUNTDOWN_SECONDS = 3
HIDE_APP_FROM_RECORDING = True   # set False if you want the window in the video

# --- palette -----------------------------------------------------------------
BG = "#0C0D10"
SURFACE = "#141519"
SURFACE_HI = "#1C1E24"
LINE = "#24262E"
TEXT = "#EDEDF0"
MUTED = "#6E7078"
LIVE = "#E5484D"


def _font(size, weight=QFont.Weight.Normal, spacing=0.0):
    f = QFont()
    f.setFamilies(["Inter", "Segoe UI Variable Display", "Segoe UI", "Arial"])
    f.setPointSizeF(size)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def _mix(a, b, t):
    ca, cb = QColor(a), QColor(b)
    return QColor(
        int(ca.red() + (cb.red() - ca.red()) * t),
        int(ca.green() + (cb.green() - ca.green()) * t),
        int(ca.blue() + (cb.blue() - ca.blue()) * t),
    )


def _clock(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def hide_from_capture(widget) -> bool:
    """WDA_EXCLUDEFROMCAPTURE: the window keeps rendering but capture APIs skip it."""
    if os.name != "nt":
        return False
    try:
        hwnd = int(widget.winId())
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11))
    except Exception:                                               # noqa: BLE001
        return False


def show_in_capture(widget):
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), 0x00)
    except Exception:                                               # noqa: BLE001
        pass


# --- audio -------------------------------------------------------------------
def input_devices():
    """Microphones and other capture devices, deduplicated by name."""
    if not AUDIO_AVAILABLE:
        return []
    found, seen = [], set()
    try:
        for index, dev in enumerate(sd.query_devices()):
            name = dev["name"].strip()
            if dev["max_input_channels"] > 0 and name.lower() not in seen:
                seen.add(name.lower())
                found.append((index, name[:38]))
    except Exception:                                               # noqa: BLE001
        return []
    return found


def _loopback_settings():
    """WASAPI loopback lets us capture whatever the speakers are playing."""
    if not AUDIO_AVAILABLE or os.name != "nt":
        return None
    try:
        return sd.WasapiSettings(loopback=True)
    except Exception:                                               # noqa: BLE001
        return None


class WavSink:
    """Writes audio blocks to disk from a worker thread, off the audio callback."""

    def __init__(self, path, channels, samplerate):
        self.q = queue.Queue(maxsize=512)
        self.wf = wave.open(path, "wb")
        self.wf.setnchannels(channels)
        self.wf.setsampwidth(2)
        self.wf.setframerate(samplerate)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        while True:
            chunk = self.q.get()
            if chunk is None:
                break
            self.wf.writeframes(chunk)

    def push(self, data):
        try:
            self.q.put_nowait(data)
        except queue.Full:
            pass  # drop a block rather than stall the audio callback

    def close(self):
        self.q.put(None)
        self.thread.join(timeout=8)
        self.wf.close()


class AudioTrack:
    def __init__(self, path, device, channels, extra_settings=None):
        self.path = path
        self.sink = WavSink(path, channels, SAMPLE_RATE)
        self.stream = sd.InputStream(
            device=device, channels=channels, samplerate=SAMPLE_RATE,
            dtype="int16", blocksize=1024, extra_settings=extra_settings,
            callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status):         # noqa: ARG002
        self.sink.push(indata.tobytes())

    def start(self):
        self.stream.start()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        finally:
            self.sink.close()


def build_tracks(mode, mic_device, tmp_prefix):
    """Returns (tracks, warning). Never raises: audio failing must not kill a capture."""
    if mode == "off" or not AUDIO_AVAILABLE:
        return [], "" if mode == "off" else "sounddevice is not installed, audio skipped"

    tracks, warnings = [], []
    if mode in ("mic", "both"):
        try:
            info = sd.query_devices(mic_device)
            channels = min(2, max(1, info["max_input_channels"]))
            track = AudioTrack(f"{tmp_prefix}_mic.wav", mic_device, channels)
            track.start()
            tracks.append(track)
        except Exception as exc:                                    # noqa: BLE001
            warnings.append(f"microphone unavailable ({exc})")

    if mode in ("system", "both"):
        settings = _loopback_settings()
        if settings is None:
            warnings.append("system audio needs Windows WASAPI loopback")
        else:
            try:
                out_device = sd.default.device[1]
                info = sd.query_devices(out_device)
                channels = min(2, max(1, info["max_output_channels"]))
                track = AudioTrack(
                    f"{tmp_prefix}_sys.wav", out_device, channels, extra_settings=settings
                )
                track.start()
                tracks.append(track)
            except Exception as exc:                                # noqa: BLE001
                warnings.append(f"system audio unavailable ({exc})")

    return tracks, " · ".join(warnings)


def mux(video_path, wav_paths):
    """Copies the video stream and attaches the audio. Returns True on success."""
    if not wav_paths or not shutil.which("ffmpeg"):
        return False
    merged = f"{os.path.splitext(video_path)[0]}_muxed.mp4"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", video_path]
    for path in wav_paths:
        cmd += ["-i", path]

    if len(wav_paths) == 1:
        cmd += ["-map", "0:v", "-map", "1:a"]
    else:
        inputs = "".join(f"[{i}:a]" for i in range(1, len(wav_paths) + 1))
        cmd += [
            "-filter_complex",
            f"{inputs}amix=inputs={len(wav_paths)}:duration=longest:normalize=0[a]",
            "-map", "0:v", "-map", "[a]",
        ]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", merged]

    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags, check=False)
    if result.returncode == 0 and os.path.exists(merged):
        os.replace(merged, video_path)
        return True
    if os.path.exists(merged):
        os.remove(merged)
    return False


# --- capture -----------------------------------------------------------------
@dataclass
class Config:
    monitor: int
    mode: str            # "record" | "lapse"
    multiplier: int
    scale: float
    fps: int
    out_dir: str
    audio_mode: str = "off"
    mic_device: int = None


class FfmpegWriter:
    def __init__(self, path, size, fps):
        w, h = size
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgra",
            "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", path,
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags,
        )

    def write(self, frame):
        self.proc.stdin.write(frame.tobytes())

    def close(self):
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        self.proc.wait(timeout=60)


class CvWriter:
    def __init__(self, path, size, fps):
        self.w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
        if not self.w.isOpened():
            raise RuntimeError("OpenCV could not open a video writer")

    def write(self, frame):
        self.w.write(frame)

    def close(self):
        self.w.release()


class CaptureWorker(QThread):
    tick = pyqtSignal(float, int)
    stage = pyqtSignal(str)
    warned = pyqtSignal(str)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        cfg = self.cfg
        writer = None
        tracks = []
        path = ""
        try:
            os.makedirs(cfg.out_dir, exist_ok=True)
            tag = "lapse" if cfg.mode == "lapse" else "record"
            speed = f"-{cfg.multiplier}x" if cfg.mode == "lapse" else ""
            stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            path = os.path.join(cfg.out_dir, f"{tag}{speed}_{stamp}.mp4")
            tmp_prefix = os.path.splitext(path)[0]

            if cfg.mode == "record":
                tracks, warning = build_tracks(cfg.audio_mode, cfg.mic_device, tmp_prefix)
                if warning:
                    self.warned.emit(warning)

            with mss.MSS() as sct:
                mon = sct.monitors[cfg.monitor]
                w = int(mon["width"] * cfg.scale) // 2 * 2
                h = int(mon["height"] * cfg.scale) // 2 * 2
                resize = (w, h) != (mon["width"], mon["height"])

                # With ffmpeg we pipe the raw BGRA straight through: skipping the
                # per-frame colour conversion is worth a lot on large displays.
                use_ffmpeg = bool(shutil.which("ffmpeg"))
                Writer = FfmpegWriter if use_ffmpeg else CvWriter
                writer = Writer(path, (w, h), cfg.fps)

                interval = (cfg.multiplier / cfg.fps) if cfg.mode == "lapse" else (1.0 / cfg.fps)
                start = time.perf_counter()
                next_frame = start
                frames = 0
                duplicated = 0
                last_tick = 0.0

                while not self._stop:
                    frame = np.asarray(sct.grab(mon))
                    if resize:
                        frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
                    if not use_ffmpeg:
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    writer.write(np.ascontiguousarray(frame))
                    frames += 1

                    now = time.perf_counter()
                    if now - last_tick >= 0.2:
                        last_tick = now
                        self.tick.emit(now - start, frames)

                    next_frame += interval

                    # If the machine cannot keep up, repeat the last frame to fill
                    # the missing slots. Identical frames cost the encoder almost
                    # nothing and it keeps the video in sync with the audio.
                    behind = time.perf_counter() - next_frame
                    if cfg.mode == "record" and behind > interval:
                        missing = min(int(behind / interval), 90)
                        for _ in range(missing):
                            writer.write(np.ascontiguousarray(frame))
                        frames += missing
                        duplicated += missing
                        next_frame += missing * interval

                    while not self._stop:
                        remaining = next_frame - time.perf_counter()
                        if remaining <= 0:
                            break
                        time.sleep(min(remaining, 0.05))
                    if cfg.mode == "lapse" and time.perf_counter() - next_frame > interval:
                        next_frame = time.perf_counter()   # lapse: never catch up

                self.tick.emit(time.perf_counter() - start, frames)
                if frames and duplicated / frames > 0.15:
                    self.warned.emit(
                        f"{duplicated * 100 // frames}% of frames were repeated, "
                        f"try a lower Quality"
                    )
        except Exception as exc:                                    # noqa: BLE001
            self._teardown(writer, tracks)
            self.failed.emit(str(exc))
            return

        wav_paths = self._teardown(writer, tracks)
        if wav_paths:
            self.stage.emit("Adding audio")
            if not mux(path, wav_paths):
                self.warned.emit("audio recorded but ffmpeg could not merge it")
            for wav in wav_paths:
                try:
                    os.remove(wav)
                except OSError:
                    pass
        self.done.emit(path)

    @staticmethod
    def _teardown(writer, tracks):
        wav_paths = []
        for track in tracks:
            try:
                track.stop()
                wav_paths.append(track.path)
            except Exception:                                       # noqa: BLE001
                pass
        if writer is not None:
            try:
                writer.close()
            except Exception:                                       # noqa: BLE001
                pass
        return wav_paths


# --- widgets -----------------------------------------------------------------
class SegmentedControl(QWidget):
    changed = pyqtSignal(int)

    def __init__(self, labels, parent=None):
        super().__init__(parent)
        self._labels = labels
        self._index = 0
        self._slide = 0.0
        self.setFixedHeight(46)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"slide", self)
        self._anim.setDuration(340)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_slide(self):
        return self._slide

    def set_slide(self, value):
        self._slide = value
        self.update()

    slide = pyqtProperty(float, get_slide, set_slide)

    def index(self):
        return self._index

    def set_index(self, index):
        if index == self._index:
            return
        self._index = index
        self._anim.stop()
        self._anim.setStartValue(self._slide)
        self._anim.setEndValue(float(index))
        self._anim.start()
        self.changed.emit(index)

    def mousePressEvent(self, event):
        self.set_index(0 if event.position().x() < self.width() / 2 else 1)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(SURFACE))
        p.drawRoundedRect(rect, 12, 12)

        pad = 4.0
        seg_w = (rect.width() - pad * 2) / 2
        ind = QRectF(pad + self._slide * seg_w, pad, seg_w, rect.height() - pad * 2)
        p.setBrush(QColor(SURFACE_HI))
        p.drawRoundedRect(ind, 9, 9)
        p.setPen(QPen(QColor(LINE), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(ind.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)

        p.setFont(_font(9.5, QFont.Weight.Medium, 1.2))
        for i, label in enumerate(self._labels):
            active = 1.0 - min(1.0, abs(self._slide - i))
            p.setPen(_mix(MUTED, TEXT, active))
            box = QRectF(pad + i * seg_w, pad, seg_w, rect.height() - pad * 2)
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label.upper())


class Chip(QWidget):
    picked = pyqtSignal(object)

    def __init__(self, value, text, parent=None):
        super().__init__(parent)
        self.value = value
        self.text_ = text
        self._on = False
        self._t = 0.0
        self.setFixedHeight(34)
        self.setMinimumWidth(50)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"t", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_t(self):
        return self._t

    def set_t(self, value):
        self._t = value
        self.update()

    t = pyqtProperty(float, get_t, set_t)

    def _to(self, target):
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(target)
        self._anim.start()

    def set_on(self, on):
        self._on = on
        self._to(1.0 if on else 0.0)

    def enterEvent(self, _):
        if not self._on and self.isEnabled():
            self._to(0.35)

    def leaveEvent(self, _):
        if not self._on:
            self._to(0.0)

    def mousePressEvent(self, _):
        if self.isEnabled():
            self.picked.emit(self.value)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        dim = 1.0 if self.isEnabled() else 0.4
        p.setPen(QPen(_mix(LINE, TEXT, self._t * 0.9 * dim), 1))
        p.setBrush(_mix(BG, SURFACE_HI, self._t))
        p.drawRoundedRect(rect, 9, 9)
        p.setFont(_font(9.5, QFont.Weight.Medium))
        p.setPen(_mix(MUTED, TEXT, self._t * dim))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text_)


class RecordButton(QWidget):
    pressed_ = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(96, 96)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._morph = 0.0
        self._pulse = 0.0

        self._morph_anim = QPropertyAnimation(self, b"morph", self)
        self._morph_anim.setDuration(420)
        self._morph_anim.setEasingCurve(QEasingCurve.Type.OutBack)

        self._pulse_anim = QPropertyAnimation(self, b"pulse", self)
        self._pulse_anim.setDuration(1900)
        self._pulse_anim.setStartValue(0.0)
        self._pulse_anim.setEndValue(1.0)
        self._pulse_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._pulse_anim.setLoopCount(-1)

    def get_morph(self):
        return self._morph

    def set_morph(self, value):
        self._morph = value
        self.update()

    morph = pyqtProperty(float, get_morph, set_morph)

    def get_pulse(self):
        return self._pulse

    def set_pulse(self, value):
        self._pulse = value
        self.update()

    pulse = pyqtProperty(float, get_pulse, set_pulse)

    def set_live(self, live, pulse=True):
        self._morph_anim.stop()
        self._morph_anim.setStartValue(self._morph)
        self._morph_anim.setEndValue(1.0 if live else 0.0)
        self._morph_anim.start()
        if live and pulse:
            self._pulse_anim.start()
        else:
            self._pulse_anim.stop()
            self.set_pulse(0.0)

    def mousePressEvent(self, _):
        self.pressed_.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QRectF(self.rect()).center()
        m = self._morph

        ring_r = 44 + 3 * self._pulse * m
        ring = QColor(LIVE if m > 0.5 else LINE)
        ring.setAlphaF(0.18 + 0.5 * self._pulse * m if m > 0.5 else 1.0)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(ring, 1.4))
        p.drawEllipse(c, ring_r, ring_r)

        p.setPen(QPen(_mix(LINE, LIVE, m), 1.4))
        p.drawEllipse(c, 40.0, 40.0)

        half = 26.0 - 8.0 * m
        radius = 26.0 - 18.0 * m
        path = QPainterPath()
        path.addRoundedRect(QRectF(c.x() - half, c.y() - half, half * 2, half * 2),
                            radius, radius)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(LIVE))
        p.drawPath(path)


class Field(QWidget):
    def __init__(self, label, items, parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self.caption = QLabel(label.upper())
        self.caption.setFont(_font(8, QFont.Weight.Medium, 1.4))
        self.caption.setStyleSheet(f"color:{MUTED};")

        self.combo = QComboBox()
        self.combo.addItems(items or ["—"])
        self.combo.setEnabled(bool(items))
        self.combo.setFont(_font(9.5))
        self.combo.setFixedHeight(38)
        self.combo.setStyleSheet(f"""
            QComboBox {{
                background:{SURFACE}; color:{TEXT}; border:1px solid {LINE};
                border-radius:9px; padding-left:12px; padding-right:12px;
            }}
            QComboBox:hover {{ border-color:{MUTED}; }}
            QComboBox:disabled {{ color:{MUTED}; border-color:{LINE}; }}
            QComboBox::drop-down {{ border:none; width:26px; }}
            QComboBox QAbstractItemView {{
                background:{SURFACE}; color:{TEXT}; border:1px solid {LINE};
                selection-background-color:{SURFACE_HI}; outline:none; padding:4px;
            }}
        """)
        box.addWidget(self.caption)
        box.addWidget(self.combo)


class Panel(QWidget):
    """A section that slides open and fades in."""

    def __init__(self, open_height, parent=None):
        super().__init__(parent)
        self.open_height = open_height
        self.setMaximumHeight(0)
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(0.0)
        self.setGraphicsEffect(self._fx)
        self._h = QPropertyAnimation(self, b"maximumHeight", self)
        self._h.setDuration(320)
        self._h.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._o = QPropertyAnimation(self._fx, b"opacity", self)
        self._o.setDuration(280)

    def reveal(self, show, animate=True):
        target_h = self.open_height if show else 0
        target_o = 1.0 if show else 0.0
        if not animate:
            self.setMaximumHeight(target_h)
            self._fx.setOpacity(target_o)
            return
        for anim, start, end in ((self._h, self.maximumHeight(), target_h),
                                 (self._o, self._fx.opacity(), target_o)):
            anim.stop()
            anim.setStartValue(start)
            anim.setEndValue(end)
            anim.start()


# --- overlays ----------------------------------------------------------------
class Overlay(QWidget):
    """Frameless, click-through-ish, always on top, never in the recording."""

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.excluded = False

    def present(self):
        self.show()
        self.excluded = hide_from_capture(self)
        if not self.excluded:
            # We cannot guarantee it stays out of the video, so don't show it.
            self.hide()
        return self.excluded


class Countdown(Overlay):
    finished = pyqtSignal()
    aborted = pyqtSignal()

    def __init__(self, seconds=COUNTDOWN_SECONDS):
        super().__init__()
        self.setFixedSize(220, 220)
        self.remaining = seconds
        self._scale = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._anim = QPropertyAnimation(self, b"scale", self)
        self._anim.setDuration(600)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_scale(self):
        return self._scale

    def set_scale(self, value):
        self._scale = value
        self.update()

    scale = pyqtProperty(float, get_scale, set_scale)

    def start_on(self, screen_geometry):
        centre = screen_geometry.center()
        self.move(centre.x() - self.width() // 2, centre.y() - self.height() // 2)
        self.show()
        hide_from_capture(self)   # nice to have here, not required
        self._bounce()
        self._timer.start(1000)

    def _bounce(self):
        self._anim.stop()
        self._anim.setStartValue(0.55)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def _step(self):
        self.remaining -= 1
        if self.remaining <= 0:
            self._timer.stop()
            self.hide()
            self.finished.emit()
            return
        self._bounce()
        self.update()

    def cancel(self):
        self._timer.stop()
        self.hide()
        self.aborted.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QRectF(self.rect()).center()
        r = 62 + 24 * (1.0 - self._scale)

        halo = QColor(TEXT)
        halo.setAlphaF(0.10 * self._scale)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, r + 26, r + 26)

        p.setBrush(QColor(12, 13, 16, 225))
        p.setPen(QPen(QColor(255, 255, 255, 40), 1.2))
        p.drawEllipse(c, r, r)

        p.setFont(_font(44, QFont.Weight.Light))
        colour = QColor(TEXT)
        colour.setAlphaF(min(1.0, self._scale))
        p.setPen(colour)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self.remaining))


class Hud(Overlay):
    """Live timer pill. Excluded from capture, so it never lands in the file."""

    stop_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setFixedSize(210, 46)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.elapsed = 0.0
        self.output = 0.0
        self._blink = 0.0
        self._anim = QPropertyAnimation(self, b"blink", self)
        self._anim.setDuration(1600)
        self._anim.setStartValue(0.25)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._anim.setLoopCount(-1)

    def get_blink(self):
        return self._blink

    def set_blink(self, value):
        self._blink = value
        self.update()

    blink = pyqtProperty(float, get_blink, set_blink)

    def place_on(self, screen_geometry):
        self.move(screen_geometry.right() - self.width() - 26, screen_geometry.top() + 26)

    def start(self):
        ok = self.present()
        if ok:
            self._anim.start()
        return ok

    def finish(self):
        self._anim.stop()
        self.hide()

    def update_times(self, elapsed, output):
        self.elapsed, self.output = elapsed, output
        self.update()

    def mousePressEvent(self, _):
        self.stop_requested.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QColor(12, 13, 16, 220))
        p.setPen(QPen(QColor(255, 255, 255, 34), 1))
        p.drawRoundedRect(rect, 14, 14)

        dot = QColor(LIVE)
        dot.setAlphaF(self._blink)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dot)
        p.drawEllipse(QRectF(18, rect.center().y() - 4, 8, 8))

        p.setFont(_font(12, QFont.Weight.Medium))
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(36, 0, 74, rect.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   _clock(self.elapsed))

        p.setFont(_font(10))
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(112, 0, 86, rect.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   f"→ {_clock(self.output)}")


# --- window ------------------------------------------------------------------
class Lapse(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.countdown = None
        self.last_path = ""
        self.out_dir = os.path.join(os.path.expanduser("~"), "Videos", APP_NAME)
        self.multiplier = 10
        self.audio_mode = "off"
        self._elapsed = 0.0
        self._frames = 0
        self._warning = ""
        self._drag = None

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(440, 720)

        with mss.MSS() as sct:
            self.monitors = [dict(m) for m in sct.monitors]
        self.mics = input_devices()

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 20, 28, 24)
        root.setSpacing(0)

        root.addLayout(self._title_bar())
        root.addSpacing(24)

        self.segment = SegmentedControl(["Record", "Time lapse"])
        self.segment.changed.connect(self._mode_changed)
        root.addWidget(self.segment)

        root.addWidget(self._build_audio_panel())
        root.addWidget(self._build_speed_panel())

        root.addSpacing(22)
        settings = QHBoxLayout()
        settings.setSpacing(14)
        self.source = Field("Source", self._monitor_names())
        self.quality = Field("Quality", ["Full resolution", "75%", "50%"])
        settings.addWidget(self.source, 3)
        settings.addWidget(self.quality, 2)
        root.addLayout(settings)

        root.addStretch(1)

        row = QHBoxLayout()
        self.button = RecordButton()
        self.button.pressed_.connect(self._toggle)
        row.addStretch(1)
        row.addWidget(self.button)
        row.addStretch(1)
        root.addLayout(row)

        root.addSpacing(20)
        self.readout = QLabel("00:00")
        self.readout.setFont(_font(26, QFont.Weight.Light))
        self.readout.setStyleSheet(f"color:{TEXT};")
        self.readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.readout)

        root.addSpacing(6)
        self.detail = QLabel("Ready")
        self.detail.setFont(_font(9))
        self.detail.setStyleSheet(f"color:{MUTED};")
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.detail)

        root.addStretch(1)

        self.footer = QLabel(self._footer_text())
        self.footer.setFont(_font(8.5))
        self.footer.setStyleSheet(f"color:{MUTED};")
        self.footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.footer.setCursor(Qt.CursorShape.PointingHandCursor)
        self.footer.mousePressEvent = lambda _: self._open_folder()
        root.addWidget(self.footer)

        self.hud = Hud()
        self.hud.stop_requested.connect(self._toggle)

        self.audio_panel.reveal(True, animate=False)

    # -- construction helpers
    def _monitor_names(self):
        real = self.monitors[1:]
        names = [f"Display {i}  ·  {m['width']}×{m['height']}" for i, m in enumerate(real, 1)]
        if len(real) > 1:
            total = self.monitors[0]
            return [f"All displays  ·  {total['width']}×{total['height']}"] + names
        return names

    def _monitor_index(self):
        """Combo index -> mss monitor index."""
        if len(self.monitors) - 1 > 1:
            return self.source.combo.currentIndex()          # 0 = the union
        return 1

    def _screen_geometry(self):
        screens = QGuiApplication.screens()
        index = self._monitor_index()
        if index >= 1 and len(screens) >= index:
            return screens[index - 1].geometry()
        return QGuiApplication.primaryScreen().geometry()

    def _build_audio_panel(self):
        self.audio_panel = Panel(140)
        box = QVBoxLayout(self.audio_panel)
        box.setContentsMargins(0, 18, 0, 0)
        box.setSpacing(10)

        cap = QLabel("AUDIO")
        cap.setFont(_font(8, QFont.Weight.Medium, 1.4))
        cap.setStyleSheet(f"color:{MUTED};")
        box.addWidget(cap)

        chips = QHBoxLayout()
        chips.setSpacing(7)
        self.audio_chips = []
        options = [("off", "Off"), ("mic", "Mic"), ("system", "System"), ("both", "Both")]
        for value, label in options:
            chip = Chip(value, label)
            chip.picked.connect(self._pick_audio)
            chip.set_on(value == self.audio_mode)
            if value != "off" and not AUDIO_AVAILABLE:
                chip.setEnabled(False)
            self.audio_chips.append(chip)
            chips.addWidget(chip)
        box.addLayout(chips)

        self.mic_field = Field("Input device", [name for _, name in self.mics])
        self.mic_field.combo.setEnabled(False)
        box.addWidget(self.mic_field)
        return self.audio_panel

    def _build_speed_panel(self):
        self.speed_panel = Panel(88)
        box = QVBoxLayout(self.speed_panel)
        box.setContentsMargins(0, 18, 0, 0)
        box.setSpacing(10)

        cap = QLabel("SPEED")
        cap.setFont(_font(8, QFont.Weight.Medium, 1.4))
        cap.setStyleSheet(f"color:{MUTED};")
        box.addWidget(cap)

        chips = QHBoxLayout()
        chips.setSpacing(7)
        self.speed_chips = []
        for value in SPEEDS:
            chip = Chip(value, f"{value}x")
            chip.picked.connect(self._pick_speed)
            chip.set_on(value == self.multiplier)
            self.speed_chips.append(chip)
            chips.addWidget(chip)
        box.addLayout(chips)
        return self.speed_panel

    def _title_bar(self):
        bar = QHBoxLayout()
        bar.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setFont(_font(10, QFont.Weight.Medium, 1.0))
        title.setStyleSheet(f"color:{TEXT};")
        bar.addWidget(title)
        bar.addStretch(1)
        for glyph, slot in (("–", self.showMinimized), ("×", self.close)):
            btn = QLabel(glyph)
            btn.setFont(_font(13))
            btn.setFixedSize(QSize(28, 24))
            btn.setAlignment(Qt.AlignmentFlag.AlignCenter)
            btn.setStyleSheet(f"color:{MUTED};")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.mousePressEvent = lambda _e, s=slot: s()
            btn.enterEvent = lambda _e, b=btn: b.setStyleSheet(f"color:{TEXT};")
            btn.leaveEvent = lambda _e, b=btn: b.setStyleSheet(f"color:{MUTED};")
            bar.addWidget(btn)
        return bar

    # -- chrome
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QColor(BG))
        p.setPen(QPen(QColor(LINE), 1))
        p.drawRoundedRect(rect, 16, 16)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _):
        self._drag = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self._toggle()
        elif event.key() == Qt.Key.Key_Escape and not self.worker and not self.countdown:
            self.close()

    # -- state
    def _mode_changed(self, index):
        lapse = index == 1
        self.speed_panel.reveal(lapse)
        self.audio_panel.reveal(not lapse)
        self._refresh_detail()

    def _pick_speed(self, value):
        self.multiplier = value
        for chip in self.speed_chips:
            chip.set_on(chip.value == value)
        self._refresh_detail()

    def _pick_audio(self, value):
        self.audio_mode = value
        for chip in self.audio_chips:
            chip.set_on(chip.value == value)
        self.mic_field.combo.setEnabled(value in ("mic", "both") and bool(self.mics))
        self._refresh_detail()

    def _footer_text(self):
        engine = "h.264" if shutil.which("ffmpeg") else "mp4v"
        displays = len(self.monitors) - 1
        return f"{displays} display · {engine} · {self.out_dir}"

    def _audio_summary(self):
        return {"off": "no audio", "mic": "microphone",
                "system": "system audio", "both": "mic + system"}[self.audio_mode]

    def _refresh_detail(self):
        if self.worker or self.countdown:
            return
        if self.segment.index() == 1:
            every = self.multiplier / OUTPUT_FPS
            self.detail.setText(
                f"1 frame every {every:.1f}s · one hour becomes {_clock(3600 / self.multiplier)}"
            )
        else:
            self.detail.setText(f"Real time at {OUTPUT_FPS} fps · {self._audio_summary()}")

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_detail()

    # -- capture flow
    def _toggle(self):
        if self.countdown:
            self.countdown.cancel()
        elif self.worker:
            self.detail.setText("Finishing the file…")
            self.worker.stop()
        else:
            self._begin_countdown()

    def _begin_countdown(self):
        self._lock_controls(True)
        self.button.set_live(True, pulse=False)
        self.readout.setText("00:00")
        self.detail.setText(f"Starting in {COUNTDOWN_SECONDS}… click again to cancel")

        self.countdown = Countdown()
        self.countdown.finished.connect(self._start)
        self.countdown.aborted.connect(self._abort_countdown)
        self.countdown.start_on(self._screen_geometry())

    def _abort_countdown(self):
        self.countdown = None
        self._lock_controls(False)
        self.button.set_live(False)
        self.readout.setText("00:00")
        self._refresh_detail()

    def _start(self):
        self.countdown = None
        self._warning = ""
        scale = [1.0, 0.75, 0.5][self.quality.combo.currentIndex()]
        mic_device = None
        if self.mics:
            mic_device = self.mics[self.mic_field.combo.currentIndex()][0]

        lapse = self.segment.index() == 1
        cfg = Config(
            monitor=self._monitor_index(),
            mode="lapse" if lapse else "record",
            multiplier=self.multiplier if lapse else 1,
            scale=scale,
            fps=OUTPUT_FPS,
            out_dir=self.out_dir,
            audio_mode="off" if lapse else self.audio_mode,
            mic_device=mic_device,
        )

        self.worker = CaptureWorker(cfg, self)
        self.worker.tick.connect(self._on_tick)
        self.worker.stage.connect(self.detail.setText)
        self.worker.warned.connect(self._on_warning)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

        self.button.set_live(True)
        self.detail.setText("Recording")

        if HIDE_APP_FROM_RECORDING:
            hide_from_capture(self)
        self.hud.place_on(self._screen_geometry())
        if not self.hud.start():
            self.detail.setText("Recording · on-screen timer needs Windows 10 2004+")

    def _on_tick(self, elapsed, frames):
        self._elapsed, self._frames = elapsed, frames
        output = frames / OUTPUT_FPS
        self.readout.setText(_clock(elapsed))
        self.detail.setText(f"{_clock(output)} of footage · {frames} frames")
        self.hud.update_times(elapsed, output)

    def _on_warning(self, message):
        self._warning = message

    def _on_done(self, path):
        self.last_path = path
        self._reset()
        size = os.path.getsize(path) / 1_048_576 if os.path.exists(path) else 0
        self.readout.setText(_clock(self._frames / OUTPUT_FPS))
        note = f" · {self._warning}" if self._warning else ""
        self.detail.setText(f"Saved · {size:.1f} MB · click the path to open{note}")

    def _on_failed(self, message):
        self._reset()
        self.readout.setText("00:00")
        self.detail.setText(f"Capture stopped: {message}")

    def _reset(self):
        self.worker = None
        self.hud.finish()
        if HIDE_APP_FROM_RECORDING:
            show_in_capture(self)
        self.button.set_live(False)
        self._lock_controls(False)

    def _lock_controls(self, locked):
        self.segment.setEnabled(not locked)
        self.source.setEnabled(not locked)
        self.quality.setEnabled(not locked)
        for chip in self.speed_chips + self.audio_chips:
            chip.setEnabled(not locked)
            chip.update()
        self.mic_field.combo.setEnabled(
            not locked and self.audio_mode in ("mic", "both") and bool(self.mics)
        )

    def _open_folder(self):
        os.makedirs(self.out_dir, exist_ok=True)
        if os.name == "nt":
            if self.last_path and os.path.exists(self.last_path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(self.last_path)])
            else:
                os.startfile(self.out_dir)                           # noqa: S606
        else:
            subprocess.Popen(["xdg-open", self.out_dir])

    def closeEvent(self, event):
        if self.countdown:
            self.countdown.cancel()
        if self.worker:
            self.worker.stop()
            self.worker.wait(20000)
        self.hud.finish()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = Lapse()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
