import contextlib
import threading
import unittest
from unittest import mock

from backend.registration import engine


class RegistrationCancellationTests(unittest.TestCase):
    def test_stop_during_preflight_prevents_later_network_checks(self):
        controller = engine.RegistrationStopController()

        def preflight(_config, get, _post):
            controller.stop()
            get("https://example.invalid")

        with (
            mock.patch.object(engine, "RegistrationStopController", return_value=controller),
            mock.patch.object(engine, "_cleanup_stale_profiles"),
            mock.patch.object(engine, "registration_log"),
            mock.patch.object(engine._conn, "run_connectivity_checks", side_effect=preflight),
            mock.patch.object(engine, "http_get") as get,
            mock.patch.object(engine, "start_browser") as start,
        ):
            engine.run_registration(1)
        get.assert_not_called()
        start.assert_not_called()

    def test_stop_during_settings_wait_skips_further_browser_fetches(self):
        stopped = threading.Event()
        page = mock.Mock()
        page.url = "https://grok.com/"

        def blocked(*args):
            stopped.set()
            return "Just a moment"

        page.run_js.side_effect = blocked
        with mock.patch.object(engine, "_active_page", return_value=page):
            with self.assertRaises(engine.RegistrationCancelled):
                engine.enable_nsfw_via_browser(cancel_callback=stopped.is_set)
        self.assertLessEqual(page.run_js.call_count, 2)

    def test_cancelled_exception_paths_never_call_mailbox_network_settlement(self):
        for workers in (1, 2):
            for error in (
                engine.RegistrationCancelled("stopped"),
                RuntimeError("Target page, context or browser has been closed"),
                engine.AccountRetryNeeded("stuck"),
                engine.EmailDomainRejected(email="fixture@example.com", message="rejected"),
            ):
                with self.subTest(workers=workers, error=type(error).__name__):
                    controllers = []

                    class Controller:
                        stopped = False

                        def __init__(self):
                            controllers.append(self)

                        def should_stop(self):
                            return self.stopped

                    def submit(**kwargs):
                        controllers[0].stopped = True
                        raise error

                    with contextlib.ExitStack() as stack:
                        stack.enter_context(mock.patch.dict(engine.config, {
                            "register_workers": workers, "account_interval": "0", "email_provider": "mailnest",
                        }))
                        for name in (
                            "start_browser", "open_signup_page", "registration_log", "prepare_mailnest_pool",
                            "_cleanup_stale_profiles", "maybe_stop_browser", "cleanup_runtime_memory",
                            "begin_sticky_proxy_account", "_announce_attempt_proxy", "clear_sticky_proxy_account",
                        ):
                            stack.enter_context(mock.patch.object(engine, name))
                        stack.enter_context(mock.patch.object(engine._conn, "run_connectivity_checks", return_value=[]))
                        stack.enter_context(mock.patch.object(engine, "RegistrationStopController", Controller))
                        stack.enter_context(mock.patch.object(engine, "fill_email_and_submit", side_effect=submit))
                        stack.enter_context(mock.patch.object(engine, "current_attempt_email", return_value="fixture@example.com"))
                        stack.enter_context(mock.patch.object(engine, "persist_registration_result"))
                        defer = stack.enter_context(mock.patch.object(engine, "defer_cancelled_mailnest_email"))
                        settle = stack.enter_context(mock.patch.object(engine, "settle_mailnest_email"))
                        engine.run_registration(workers)
                    settle.assert_not_called()
                    self.assertTrue(defer.called)

