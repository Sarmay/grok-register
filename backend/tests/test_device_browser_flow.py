"""浏览器 Device Flow：页面动作判断，以及授权与 token 轮询并行。"""

import threading
import time
import unittest
from unittest import mock

from backend.integrations import auth_exchange
from backend.registration.signup_flow import (
    DEVICE_BROWSER_TIMEOUT_SECONDS,
    _click_device_button_exact,
    device_cookie_banner_visible,
    next_device_approval_action,
)


class DevicePageActionTests(unittest.TestCase):
    def test_click_helper_supports_role_button(self):
        class _States:
            is_displayed = True
            is_enabled = True

        class _Element:
            text = "Continue"
            states = _States()

            def attr(self, name):
                return "" if name != "aria-label" else "Continue"

            def click(self):
                self.clicked = True

        class _Page:
            def __init__(self):
                self.element = _Element()

            def eles(self, selector):
                return [self.element] if selector == '[role="button"]' else []

        page = _Page()
        self.assertEqual(_click_device_button_exact(page, frozenset({"continue"})), "Continue")
        self.assertTrue(page.element.clicked)

    def test_duplicated_button_text_still_matches_continue(self):
        body = "Enter the code shown on your device"
        buttons = ["Continue Continue"]
        self.assertEqual(
            next_device_approval_action("https://accounts.x.ai/oauth2/device?user_code=AB", body, buttons),
            "click-continue",
        )
        self.assertEqual(
            next_device_approval_action(
                "https://accounts.x.ai/oauth2/device/consent",
                "Authorize Grok Build",
                ["Allow Allow"],
            ),
            "click-allow",
        )

    def test_exact_allow_is_not_the_cookie_button(self):
        body = "Authorize Grok Build\nCookie Policy"
        buttons = ["Allow", "Deny", "Cookie Policy"]
        self.assertFalse(device_cookie_banner_visible(body))
        self.assertEqual(next_device_approval_action("https://accounts.x.ai/oauth2/device/consent", body, buttons), "click-allow")

    def test_allow_all_stays_on_the_cookie_banner(self):
        body = "Privacy preference\nAllow all cookies"
        buttons = ["Allow all", "Reject"]
        self.assertTrue(device_cookie_banner_visible(body))
        self.assertEqual(next_device_approval_action("https://accounts.x.ai/oauth2/device/consent", body, buttons), "dismiss-cookie")
        self.assertEqual(
            next_device_approval_action(
                "https://accounts.x.ai/oauth2/device/consent",
                body,
                ["Allow", "Allow all"],
                ignore_cookie=True,
            ),
            "click-allow",
        )

    def test_invalid_action_reloads_before_clicking_again(self):
        body = "Invalid action"
        buttons = ["Allow"]
        self.assertEqual(
            next_device_approval_action("https://accounts.x.ai/oauth2/device/consent", body, buttons),
            "reload",
        )
        self.assertEqual(
            next_device_approval_action("https://auth.x.ai/oauth2/device/approve", "", []),
            "reload",
        )

    def test_done_page_and_login_redirect(self):
        self.assertEqual(
            next_device_approval_action("https://accounts.x.ai/oauth2/device/done", "Device authorized", []),
            "done",
        )
        self.assertEqual(
            next_device_approval_action("https://accounts.x.ai/sign-in", "Sign in", ["Continue"]),
            "need-login",
        )
        self.assertEqual(
            next_device_approval_action(
                "https://accounts.x.ai/oauth2/device?user_code=AB",
                "Enter code",
                ["Continue"],
            ),
            "click-continue",
        )


class DevicePollWindowTests(unittest.TestCase):
    def test_protocol_path_keeps_the_short_cap(self):
        self.assertEqual(auth_exchange._device_poll_limit(1800), auth_exchange.DEVICE_POLL_CAP_SECONDS)

    def test_browser_path_uses_its_own_window(self):
        self.assertEqual(auth_exchange.DEVICE_BROWSER_TIMEOUT_SECONDS, DEVICE_BROWSER_TIMEOUT_SECONDS)
        self.assertEqual(
            auth_exchange._device_poll_limit(1800, auth_exchange.DEVICE_BROWSER_TIMEOUT_SECONDS),
            auth_exchange.DEVICE_BROWSER_TIMEOUT_SECONDS,
        )

    def test_discovery_returns_dynamic_endpoints(self):
        endpoints = {
            "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code-v2",
            "token_endpoint": "https://auth.x.ai/oauth2/token-v2",
        }
        with mock.patch.object(
            auth_exchange,
            "_device_http_request",
            return_value=(200, endpoints, "{}"),
        ) as request:
            result = auth_exchange._discover_device_endpoints(log=lambda *_args, **_kwargs: None)
        self.assertEqual(result, endpoints)
        request.assert_called_once()

    def test_poll_uses_discovered_token_endpoint(self):
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return 200, {"access_token": "tok", "refresh_token": "refresh"}, "{}"

        with mock.patch.object(auth_exchange, "_device_http_request", side_effect=request):
            token = auth_exchange.poll_device_token(
                "device-code",
                proxy="http://proxy.invalid:1",
                max_wait=1,
                token_endpoint="https://auth.x.ai/oauth2/token-v2",
                log=lambda *_args, **_kwargs: None,
            )
        self.assertEqual(token["access_token"], "tok")
        self.assertEqual(calls[0][1], "https://auth.x.ai/oauth2/token-v2")


class DeviceBrowserExchangeTests(unittest.TestCase):
    def _device(self):
        return {
            "device_code": "device-code",
            "user_code": "PSSS-KKGC",
            "interval": 5,
            "expires_in": 1800,
            "open_url": "https://accounts.x.ai/oauth2/device?user_code=PSSS-KKGC",
            "token_endpoint": "https://auth.x.ai/oauth2/token-v2",
        }

    def test_poll_runs_while_the_browser_is_still_approving(self):
        started = threading.Event()
        release = threading.Event()

        def approve(user_code, open_url, stop_event=None):
            self.assertEqual(user_code, "PSSS-KKGC")
            self.assertIn("oauth2/device", open_url)
            started.set()
            self.assertTrue(release.wait(2))
            self.assertIsNotNone(stop_event)
            self.assertTrue(stop_event.is_set())
            return True

        def poll(*_args, **kwargs):
            self.assertTrue(started.wait(2))
            self.assertEqual(kwargs.get("max_wait"), auth_exchange.DEVICE_BROWSER_TIMEOUT_SECONDS)
            self.assertEqual(kwargs.get("token_endpoint"), "https://auth.x.ai/oauth2/token-v2")
            kwargs["stop_event"].set()
            release.set()
            return {"access_token": "tok", "refresh_token": "refresh"}

        with (
            mock.patch.object(auth_exchange, "_probe_sso_session", return_value=True),
            mock.patch.object(auth_exchange, "request_device_code", return_value=self._device()),
            mock.patch.object(auth_exchange, "poll_device_token", side_effect=poll),
        ):
            token = auth_exchange.sso_to_token_device_browser(
                "sso-cookie",
                approve,
                log=lambda *_args, **_kwargs: None,
            )

        self.assertEqual(token["access_token"], "tok")

    def test_browser_failure_stops_the_poll(self):
        def approve(user_code, open_url, stop_event=None):
            return False

        def poll(*_args, **kwargs):
            deadline = time.time() + 2
            while time.time() < deadline:
                if kwargs["stop_event"].is_set():
                    return None
                time.sleep(0.02)
            self.fail("poll was not stopped")

        with (
            mock.patch.object(auth_exchange, "_probe_sso_session", return_value=True),
            mock.patch.object(auth_exchange, "request_device_code", return_value=self._device()),
            mock.patch.object(auth_exchange, "poll_device_token", side_effect=poll),
        ):
            token = auth_exchange.sso_to_token_device_browser(
                "sso-cookie",
                approve,
                log=lambda *_args, **_kwargs: None,
            )

        self.assertIsNone(token)

    def test_tls_probe_retries_then_accepts_sso(self):
        calls = {"count": 0}

        class _Response:
            url = "https://accounts.x.ai/"
            status_code = 200

        class _Session:
            def get(self, *_args, **_kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("curl: (35) TLS connect error: OPENSSL_internal:invalid library")
                return _Response()

            def close(self):
                return None

        with (
            mock.patch.object(auth_exchange, "_new_sso_session", return_value=_Session()),
            mock.patch.object(auth_exchange.time, "sleep"),
        ):
            self.assertTrue(auth_exchange._probe_sso_session("sso", log=lambda *_args, **_kwargs: None))
        self.assertEqual(calls["count"], 2)

    def test_browser_mode_skips_protocol_approve_before_auth_code(self):
        calls = []

        def browser(*_args, **_kwargs):
            calls.append("browser")
            return None

        def protocol(*_args, **_kwargs):
            calls.append("protocol")
            self.fail("browser mode must not enter protocol approve fallback")

        def auth_code(*_args, **_kwargs):
            calls.append("auth_code")
            return {"access_token": "tok"}

        with (
            mock.patch.object(auth_exchange, "sso_to_token_device_browser", side_effect=browser),
            mock.patch.object(auth_exchange, "sso_to_token_device_flow", side_effect=protocol),
            mock.patch.object(auth_exchange, "sso_to_token_auth_code", side_effect=auth_code),
        ):
            token = auth_exchange.sso_to_token(
                "sso-cookie",
                browser_approve=lambda *_args, **_kwargs: False,
                log=lambda *_args, **_kwargs: None,
            )
        self.assertEqual(calls, ["browser", "browser", "auth_code"])
        self.assertEqual(token["access_token"], "tok")


if __name__ == "__main__":
    unittest.main()
