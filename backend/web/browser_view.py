"""把容器里的 Xvfb 虚拟屏幕抓成 JPEG，供运行监控页查看浏览器窗口。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Mapping, Optional

_DISPLAY_RE = re.compile(r"^:\d+(?:\.\d+)?$")
_SCREEN_RE = re.compile(r"(\d+)x(\d+)")
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_MAX_BUFFER = 8_000_000
_IDLE_SECONDS = 20.0
_RETRY_SECONDS = 5.0


def parse_screen_size(value: str, default: tuple[int, int] = (1920, 1080)) -> tuple[int, int]:
    match = _SCREEN_RE.search(str(value or ""))
    if not match:
        return default
    width, height = int(match.group(1)), int(match.group(2))
    if width < 320 or height < 240 or width > 7680 or height > 4320:
        return default
    return width, height


def extract_jpeg_frames(buffer: bytearray) -> list[bytes]:
    """从增量字节里取出完整 JPEG，并把未完成的尾部留在 buffer 中。"""
    frames: list[bytes] = []
    while True:
        start = buffer.find(_JPEG_SOI)
        if start < 0:
            if len(buffer) > 1:
                del buffer[:-1]
            break
        if start:
            del buffer[:start]
        end = buffer.find(_JPEG_EOI, 2)
        if end < 0:
            if len(buffer) > _MAX_BUFFER:
                del buffer[:-1]
            break
        frames.append(bytes(buffer[: end + 2]))
        del buffer[: end + 2]
    return frames


def build_ffmpeg_command(ffmpeg: str, display: str, size: tuple[int, int]) -> list[str]:
    width, height = size
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "x11grab",
        "-draw_mouse",
        "1",
        "-framerate",
        "4",
        "-video_size",
        f"{width}x{height}",
        "-i",
        display,
        "-an",
        "-vf",
        "scale=1280:-2",
        "-q:v",
        "6",
        "-f",
        "mjpeg",
        "pipe:1",
    ]


class DisplayGrabber:
    """有人看监控页时才启动 ffmpeg，离开后自动停掉。"""

    def __init__(
        self,
        env: Optional[Mapping[str, str]] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
        popen: Optional[Callable[..., subprocess.Popen]] = None,
    ):
        self._env = env if env is not None else os.environ
        self._which = which or shutil.which
        self._popen = popen or subprocess.Popen
        self._lock = threading.Lock()
        self._frame = b""
        self._updated_at = 0.0
        self._started_at = 0.0
        self._error = ""
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_touch = 0.0
        self._retry_after = 0.0

    def display(self) -> str:
        value = str(self._env.get("DISPLAY") or "").strip()
        return value if _DISPLAY_RE.match(value) else ""

    def ffmpeg(self) -> str:
        return str(self._which("ffmpeg") or "").strip()

    def screen_size(self) -> tuple[int, int]:
        return parse_screen_size(str(self._env.get("XVFB_SCREEN") or ""))

    def unavailable_reason(self) -> str:
        if not self.display():
            return "当前没有虚拟屏幕。容器里的有头浏览器会显示在这里；本机直接运行时请看弹出的窗口。"
        if not self.ffmpeg():
            return "未找到 ffmpeg，暂时不能抓取虚拟屏幕。重新构建镜像后即可在这里查看。"
        return ""

    def status(self) -> dict:
        reason = self.unavailable_reason()
        with self._lock:
            updated_at = self._updated_at
            error = self._error
            capturing = bool(self._thread and self._thread.is_alive())
            has_frame = bool(self._frame)
        age = round(time.time() - updated_at, 1) if updated_at else None
        return {
            "enabled": not reason,
            "display": self.display(),
            "capturing": capturing,
            "has_frame": has_frame,
            "updated_at": updated_at or None,
            "age_seconds": age,
            "error": error,
            "reason": reason,
        }

    def frame(self, wait: float = 0.8) -> bytes:
        self.touch()
        deadline = time.monotonic() + max(wait, 0)
        while True:
            with self._lock:
                current = self._frame
            if current or time.monotonic() >= deadline:
                return current
            time.sleep(0.05)

    def touch(self) -> None:
        with self._lock:
            self._last_touch = time.monotonic()
            alive = bool(self._thread and self._thread.is_alive())
            fresh_at = max(self._updated_at, self._started_at)
            stalled = alive and fresh_at > 0 and time.time() - fresh_at > 3
        if stalled:
            self._stop.set()
            self._terminate_process()
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if time.monotonic() < self._retry_after or self.unavailable_reason():
                return
            self._stop.clear()
            thread = threading.Thread(target=self._run, name="browser-view", daemon=True)
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._terminate_process()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self) -> None:
        command = build_ffmpeg_command(self.ffmpeg(), self.display(), self.screen_size())
        try:
            process = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(self._env),
                bufsize=0,
            )
        except Exception as exc:
            with self._lock:
                self._error = f"无法启动画面抓取: {exc}"
                self._retry_after = time.monotonic() + _RETRY_SECONDS
            return
        with self._lock:
            self._process = process
            self._started_at = time.time()
            self._error = ""
        stderr_parts: list[bytes] = []
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process, stderr_parts),
            name="browser-view-stderr",
            daemon=True,
        )
        stderr_thread.start()
        buffer = bytearray()
        try:
            while not self._stop.is_set():
                chunk = process.stdout.read(65536) if process.stdout else b""
                if not chunk:
                    break
                buffer.extend(chunk)
                for frame in extract_jpeg_frames(buffer):
                    with self._lock:
                        self._frame = frame
                        self._updated_at = time.time()
                        idle_for = time.monotonic() - self._last_touch
                    if idle_for > _IDLE_SECONDS:
                        self._stop.set()
                        break
        finally:
            self._terminate_process()
            stderr_thread.join(timeout=1)
            detail = b"".join(stderr_parts).decode("utf-8", "replace").strip()
            code = process.poll()
            stopped = self._stop.is_set()
            with self._lock:
                self._process = None
                if stopped:
                    if not detail:
                        self._error = ""
                elif detail:
                    self._error = detail[:500]
                    self._retry_after = time.monotonic() + _RETRY_SECONDS
                elif code not in (0, None):
                    self._error = f"画面抓取已退出，代码 {code}"
                    self._retry_after = time.monotonic() + _RETRY_SECONDS

    def _read_stderr(self, process: subprocess.Popen, sink: list[bytes]) -> None:
        if not process.stderr:
            return
        try:
            data = process.stderr.read() or b""
        except Exception:
            return
        if data:
            sink.append(data[:4000])

    def _terminate_process(self) -> None:
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


_grabber: Optional[DisplayGrabber] = None
_grabber_lock = threading.Lock()


def get_browser_view() -> DisplayGrabber:
    global _grabber
    with _grabber_lock:
        if _grabber is None:
            _grabber = DisplayGrabber()
        return _grabber
