import unittest
import threading
import time
from unittest import mock

from backend.web.jobs import RegistrationJobCoordinator
from backend.registration import engine


class RegistrationJobProgressTests(unittest.TestCase):
    def test_stop_cleanup_finishes_before_next_job_can_start(self):
        manager = RegistrationJobCoordinator()
        ready = threading.Event()
        release_cleanup = threading.Event()

        def run(_count):
            controller = engine.RegistrationStopController()
            ready.set()
            while not controller.should_stop():
                time.sleep(0.01)

        with (
            mock.patch.object(manager, "_repository", return_value=None),
            mock.patch("backend.web.jobs.TaskRunRecorder"),
            mock.patch("backend.web.jobs.prune_history"),
            mock.patch.object(engine, "load_config"),
            mock.patch.object(engine, "_wire_runtime_modules"),
            mock.patch.object(engine, "run_registration", side_effect=run),
            mock.patch.object(engine._bs, "interrupt_browser_work", side_effect=lambda **_: release_cleanup.wait(2)),
            mock.patch.object(engine._bs, "allow_browser_launches") as allow,
            mock.patch.dict(engine.config, {"debug_mode": False}),
        ):
            try:
                manager.start()
                self.assertTrue(ready.wait(1))
                manager.request_stop()
                self.assertTrue(manager.status()["running"])
                with self.assertRaises(RuntimeError):
                    manager.start()
                allow.assert_called_once()
            finally:
                release_cleanup.set()
                manager._thread.join(2)
            self.assertFalse(manager.status()["running"])
            self.assertEqual(manager.status()["current_stage"], "任务已停止")
            with mock.patch.object(engine, "run_registration"):
                manager.start()
                manager._thread.join(2)
            self.assertFalse(manager.status()["running"])

    def test_stop_before_controller_is_nonblocking_and_idempotent(self):
        manager = RegistrationJobCoordinator()
        manager._running = True
        with (
            mock.patch.object(manager, "_repository", return_value=None),
            mock.patch.object(engine._bs, "interrupt_browser_work") as interrupt,
        ):
            started = time.monotonic()
            manager.stop()
            manager.stop()
            self.assertLess(time.monotonic() - started, 0.7)
            manager._interrupt_thread.join(1)
        interrupt.assert_called_once()
        self.assertTrue(manager._stop_requested_before_controller)

    def test_tracks_success_failure_stage_and_email(self):
        manager = RegistrationJobCoordinator()
        manager._target_count = 3
        manager._running = True

        manager._append_log("[*] 2. 创建邮箱并提交")
        manager._append_log("[*] 邮箱: first@example.com")
        manager._append_log("[+] 注册成功: first@example.com")
        manager._append_log("[-] 注册失败 [浏览器异常]: failed")

        status = manager.status()
        self.assertEqual(status["completed_count"], 2)
        self.assertEqual(status["success_count"], 1)
        self.assertEqual(status["failure_count"], 1)
        self.assertEqual(status["current_email"], "first@example.com")
        self.assertEqual(status["progress_percent"], 66.7)

    def test_browser_start_failure_counts_multiple_tasks_and_caps_target(self):
        manager = RegistrationJobCoordinator()
        manager._target_count = 2
        manager._append_log("[W1] [-] 浏览器启动失败，5 个任务均记为失败: boom")

        status = manager.status()
        self.assertEqual(status["completed_count"], 2)
        self.assertEqual(status["failure_count"], 2)
        self.assertEqual(status["progress_percent"], 100.0)

    def test_request_stop_interrupts_browser_work(self):
        manager = RegistrationJobCoordinator()
        manager._running = True

        class Controller:
            def __init__(self):
                self.stop_requested = False

            def stop(self):
                self.stop_requested = True

            def should_stop(self):
                return self.stop_requested

        manager._stop_controller = Controller()
        with mock.patch("backend.automation.session.interrupt_browser_work") as interrupt:
            status = manager.request_stop()

        interrupt.assert_called_once()
        self.assertTrue(manager._stop_controller.stop_requested)
        self.assertTrue(status["running"])

    def test_request_stop_does_not_wait_for_slow_browser_cleanup(self):
        manager = RegistrationJobCoordinator()
        manager._running = True

        class Controller:
            def __init__(self):
                self.stop_requested = False

            def stop(self):
                self.stop_requested = True

            def should_stop(self):
                return self.stop_requested

        manager._stop_controller = Controller()
        release = threading.Event()

        def slow_interrupt(**_kwargs):
            release.wait(1.0)

        started = time.monotonic()
        with mock.patch("backend.automation.session.interrupt_browser_work", side_effect=slow_interrupt):
            status = manager.request_stop()
        elapsed = time.monotonic() - started
        release.set()

        self.assertLess(elapsed, 0.7)
        self.assertTrue(status["running"])


if __name__ == "__main__":
    unittest.main()
