import io
import threading
import time
import unittest

from backend.web.browser_view import (
    DisplayGrabber,
    build_ffmpeg_command,
    extract_jpeg_frames,
    parse_screen_size,
)


class _FakeProcess:
    def __init__(self, payload: bytes, stderr: bytes = b""):
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class BrowserViewTests(unittest.TestCase):
    def test_parse_screen_size_uses_xvfb_text(self):
        self.assertEqual(parse_screen_size("1920x1080x24"), (1920, 1080))
        self.assertEqual(parse_screen_size("bad"), (1920, 1080))
        self.assertEqual(parse_screen_size("10x10"), (1920, 1080))

    def test_extract_jpeg_frames_keeps_a_partial_tail(self):
        buffer = bytearray(b"noise\xff\xd8one\xff\xd9\xff\xd8two")
        frames = extract_jpeg_frames(buffer)
        self.assertEqual(frames, [b"\xff\xd8one\xff\xd9"])
        self.assertEqual(bytes(buffer), b"\xff\xd8two")
        buffer.extend(b"\xff\xd9")
        self.assertEqual(extract_jpeg_frames(buffer), [b"\xff\xd8two\xff\xd9"])
        self.assertEqual(bytes(buffer), b"")

    def test_ffmpeg_command_targets_the_virtual_display(self):
        command = build_ffmpeg_command("/usr/bin/ffmpeg", ":99", (1920, 1080))
        self.assertEqual(command[0], "/usr/bin/ffmpeg")
        self.assertIn("x11grab", command)
        self.assertIn(":99", command)
        self.assertIn("1920x1080", command)
        self.assertIn("pipe:1", command)

    def test_status_explains_missing_display_and_ffmpeg(self):
        missing_display = DisplayGrabber(env={}, which=lambda name: "/usr/bin/ffmpeg")
        self.assertFalse(missing_display.status()["enabled"])
        self.assertIn("虚拟屏幕", missing_display.status()["reason"])

        missing_ffmpeg = DisplayGrabber(env={"DISPLAY": ":99"}, which=lambda name: None)
        self.assertFalse(missing_ffmpeg.status()["enabled"])
        self.assertIn("ffmpeg", missing_ffmpeg.status()["reason"])

    def test_grabber_publishes_a_jpeg_frame(self):
        payload = b"\xff\xd8hello\xff\xd9"
        created = threading.Event()

        def popen(*args, **kwargs):
            created.set()
            return _FakeProcess(payload)

        grabber = DisplayGrabber(
            env={"DISPLAY": ":99", "XVFB_SCREEN": "1280x720x24"},
            which=lambda name: "/usr/bin/ffmpeg",
            popen=popen,
        )
        frame = grabber.frame(wait=1)
        self.assertTrue(created.is_set())
        self.assertEqual(frame, payload)
        status = grabber.status()
        self.assertTrue(status["has_frame"])
        self.assertEqual(status["display"], ":99")
        grabber.stop()
