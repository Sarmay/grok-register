import unittest
from unittest import mock

from backend.registration import engine
from backend.registration import signup_flow


class SignupFlowTests(unittest.TestCase):
    class NativeInput:
        def __init__(self, current_value=""):
            self.current_value = current_value
            self.states = mock.Mock(is_alive=True, is_displayed=True, is_enabled=True)

        def click(self, **kwargs):
            return None

        def input(self, value, **kwargs):
            return None

        def property(self, name):
            return self.current_value

    def test_native_input_does_not_treat_empty_value_as_success(self):
        element = self.NativeInput(current_value="")
        self.assertFalse(signup_flow._native_type_element(element, "Neo"))

    def test_native_input_accepts_confirmed_value(self):
        element = self.NativeInput(current_value="Neo")
        self.assertTrue(signup_flow._native_type_element(element, "Neo"))

    def test_turnstile_problem_detects_verification_failed_text(self):
        page = mock.Mock()
        page.run_js.return_value = "failed"
        with mock.patch.object(signup_flow, "page", page):
            self.assertEqual(signup_flow._turnstile_widget_problem(), "failed")

    def test_turnstile_click_stops_when_widget_already_failed(self):
        page = mock.Mock()

        def run_js(script, *args):
            if "verification failed" in script:
                return "failed"
            return ""

        page.run_js.side_effect = run_js
        logs = []
        signup_flow.configure(sleep_with_cancel=lambda *args, **kwargs: None)
        with mock.patch.object(signup_flow, "page", page), mock.patch.object(
            signup_flow, "active_page", return_value=page
        ), mock.patch.object(
            signup_flow, "_try_click_turnstile_frame"
        ) as click:
            with self.assertRaises(signup_flow.TurnstileWidgetRejected):
                signup_flow.getTurnstileToken(log_callback=logs.append, max_unsolved_clicks=2)
        click.assert_not_called()

    def test_rejected_turnstile_refreshes_once_then_requests_new_exit(self):
        page = mock.Mock()
        signup_flow.configure(
            AccountRetryNeeded=engine.AccountRetryNeeded,
            sleep_with_cancel=lambda *args, **kwargs: None,
        )
        with mock.patch.object(signup_flow, "page", page):
            refreshed = signup_flow._reject_turnstile_or_refresh(
                False,
                signup_flow.TurnstileWidgetRejected("Verification failed"),
            )
            self.assertTrue(refreshed)
            page.reload.assert_called_once_with()
            with self.assertRaises(engine.AccountRetryNeeded) as caught:
                signup_flow._reject_turnstile_or_refresh(
                    True,
                    signup_flow.TurnstileWidgetRejected("Verification failed"),
                )
        self.assertTrue(caught.exception.single_retry)

    def test_turnstile_exit_retry_is_limited_to_one(self):
        exc = engine.AccountRetryNeeded("Turnstile 刷新后仍失败")
        exc.single_retry = True
        self.assertEqual(engine.account_retry_limit(exc, 3), 1)
        self.assertEqual(engine.account_retry_limit(engine.AccountRetryNeeded("卡住"), 3), 3)

    def test_nsfw_navigation_waits_and_retries_binding_abort(self):
        page = mock.Mock()
        page.url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        page.wait = mock.Mock()
        page.get.side_effect = [
            Exception("Page.goto: NS_BINDING_ABORTED; maybe frame was detached?"),
            None,
        ]
        logs = []
        with mock.patch.object(engine.time, "sleep"):
            engine._open_grok_for_nsfw(page, log_callback=logs.append)
        self.assertEqual(page.wait.doc_loaded.call_count, 2)
        page.wait.doc_loaded.assert_called_with(timeout=8)
        self.assertEqual(page.get.call_count, 2)
        self.assertTrue(any("1 秒后重试" in line for line in logs))

    def test_nsfw_navigation_skips_goto_when_already_on_grok(self):
        page = mock.Mock()
        page.url = "https://grok.com/"
        engine._open_grok_for_nsfw(page)
        page.get.assert_not_called()

    def test_detects_account_already_registered_notice(self):
        page = mock.Mock()
        page.run_js.return_value = {
            "notices": [
                "Existing account found An account already exists which is associated with this email address. Please login using the login method shown below. Login with email"
            ],
            "body": "Complete your sign up",
            "url": "https://accounts.x.ai/sign-up",
        }
        with mock.patch.object(signup_flow, "page", page):
            notice = signup_flow.detect_account_already_registered()
        self.assertTrue(notice.startswith("Existing account found"))

    def test_detects_account_already_registered_by_dom_signature(self):
        page = mock.Mock()
        page.run_js.return_value = {
            "signature": {
                "matched": True,
                "name": "existing-account-email-login-card",
                "text": "找到现有账户 已存在与此邮箱地址关联的账户。 使用邮箱登录",
            },
            "notices": [],
            "body": "",
            "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
        }
        with mock.patch.object(signup_flow, "page", page):
            notice = signup_flow.detect_account_already_registered()
        self.assertEqual(notice, "找到现有账户 已存在与此邮箱地址关联的账户。 使用邮箱登录")

    def test_detects_chinese_account_wording_as_text_fallback(self):
        page = mock.Mock()
        page.run_js.return_value = {
            "signature": {"matched": False},
            "notices": ["找到现有账户 已存在与此邮箱地址关联的账户。"],
            "body": "",
            "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
        }
        with mock.patch.object(signup_flow, "page", page):
            notice = signup_flow.detect_account_already_registered()
        self.assertTrue(notice.startswith("找到现有账户"))

    def test_known_text_takes_priority_over_dom_signature(self):
        page = mock.Mock()
        page.run_js.return_value = {
            "signature": {
                "matched": True,
                "name": "existing-account-email-login-card",
                "text": "DOM fallback result",
            },
            "notices": [
                "Existing account found An account already exists which is associated with this email address."
            ],
            "body": "",
            "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
        }
        with mock.patch.object(signup_flow, "page", page):
            notice = signup_flow.detect_account_already_registered()
        self.assertTrue(notice.startswith("Existing account found"))

    def test_ignores_generic_existing_account_signin_link(self):
        page = mock.Mock()
        page.run_js.return_value = {
            "notices": [],
            "body": "Already have an account? Sign in",
            "url": "https://accounts.x.ai/sign-up",
        }
        with mock.patch.object(signup_flow, "page", page):
            self.assertEqual(signup_flow.detect_account_already_registered(), "")

    def test_duplicate_account_has_own_failure_type(self):
        exc = signup_flow.AccountAlreadyRegistered("fixture")
        self.assertEqual(engine.classify_failure(exc), engine.FAIL_ALREADY_REGISTERED)

    def test_detects_too_many_code_requests(self):
        page = mock.Mock()
        page.run_js.return_value = (
            "Too many code requests. Please wait a few minutes before requesting another code."
        )
        signup_flow.configure(EmailCodeRateLimited=engine.EmailCodeRateLimited)
        with mock.patch.object(signup_flow, "page", page):
            with self.assertRaises(engine.EmailCodeRateLimited) as caught:
                signup_flow.raise_if_email_code_rate_limited("limited@outlook.com")
        self.assertIn("limited@outlook.com", str(caught.exception))
        self.assertEqual(engine.classify_failure(caught.exception), engine.FAIL_CODE_RATE)

    def test_email_timeout_reports_visible_controls_and_rate_limit(self):
        signup_flow.configure(EmailCodeRateLimited=engine.EmailCodeRateLimited)
        rate_limited = signup_flow.email_submit_timeout_error(
            "limited@outlook.com",
            True,
            {
                "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
                "inputs": ["type=email name=email"],
                "buttons": ["Sign up", "Go back"],
                "notice": "Too many code requests. Please wait a few minutes before requesting another code.",
            },
        )
        self.assertIsInstance(rate_limited, engine.EmailCodeRateLimited)
        stuck = signup_flow.email_submit_timeout_error(
            "limited@outlook.com",
            True,
            {
                "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
                "inputs": ["type=email name=email"],
                "buttons": ["Sign up disabled", "Go back"],
                "notice": "",
            },
        )
        self.assertIn("邮箱已填写但页面未进入验证码步骤", str(stuck))
        self.assertIn("type=email name=email", str(stuck))
        self.assertIn("Sign up disabled", str(stuck))
        repeated = signup_flow.email_submit_timeout_error(
            "limited@outlook.com",
            True,
            {
                "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
                "inputs": ["type=email name=email"],
                "buttons": ["Sign up disabled"],
            },
            received_codes=2,
        )
        self.assertIsInstance(repeated, engine.EmailCodeRateLimited)

    def test_code_submission_accepts_native_button_label(self):
        logs = []
        page = mock.Mock()
        with mock.patch.dict(
            signup_flow._deps,
            {"get_oai_code": mock.Mock(return_value="123456")},
        ), mock.patch.object(
            signup_flow, "_native_fill_code", return_value="filled-aggregate"
        ), mock.patch.object(
            signup_flow, "_native_click_action", return_value="Continue"
        ), mock.patch.object(
            signup_flow, "sleep_with_cancel"
        ), mock.patch.object(
            signup_flow, "_profile_page_snapshot", return_value={"profile_form": False}
        ), mock.patch.object(
            signup_flow,
            "_wait_profile_page_after_code",
            return_value={"profile_form": True, "url": "https://accounts.x.ai/sign-up"},
        ), mock.patch.object(signup_flow, "page", page):
            result = signup_flow.fill_code_and_submit(
                "fixture@example.com",
                "fixture-token",
                timeout=1,
                log_callback=logs.append,
            )

        self.assertEqual(result, "123456")
        self.assertFalse(page.run_js.called)
        self.assertTrue(any("Continue" in message for message in logs))

    def test_code_submission_detects_profile_page_before_refilling_otp(self):
        logs = []
        with mock.patch.dict(
            signup_flow._deps,
            {"get_oai_code": mock.Mock(return_value="123456")},
        ), mock.patch.object(
            signup_flow,
            "_profile_page_snapshot",
            return_value={"profile_form": True, "url": "https://accounts.x.ai/sign-up"},
        ), mock.patch.object(signup_flow, "_native_fill_code") as fill_code:
            result = signup_flow.fill_code_and_submit(
                "fixture@example.com",
                "fixture-token",
                timeout=1,
                log_callback=logs.append,
            )

        self.assertEqual(result, "123456")
        self.assertFalse(fill_code.called)
        self.assertTrue(any("页面元素识别资料填写页" in message for message in logs))

    def test_hyphenated_numeric_code_is_filled_without_separator(self):
        with mock.patch.dict(
            signup_flow._deps,
            {"get_oai_code": mock.Mock(return_value="134-771")},
        ), mock.patch.object(
            signup_flow,
            "_native_fill_code",
            return_value="filled-aggregate",
        ) as fill_code, mock.patch.object(
            signup_flow,
            "_native_click_action",
            return_value="Continue",
        ), mock.patch.object(
            signup_flow,
            "_profile_page_snapshot",
            return_value={"profile_form": False},
        ), mock.patch.object(
            signup_flow,
            "_wait_profile_page_after_code",
            return_value={"profile_form": True, "url": "https://accounts.x.ai/sign-up"},
        ), mock.patch.object(signup_flow, "page", mock.Mock()):
            result = signup_flow.fill_code_and_submit(
                "fixture@example.com",
                "fixture-token",
                timeout=1,
            )

        self.assertEqual(result, "134-771")
        fill_code.assert_called_once_with("134771")


class _SignupPage:
    def __init__(self, error=None, url="https://accounts.x.ai/sign-up?redirect=grok-com"):
        self.error = error
        self.url = url
        self.calls = []
        self.run_js_result = {
            "url": url,
            "text": "Sign up with email",
            "ready": True,
            "email_form": False,
            "signup_action": True,
        }

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error

    def run_js(self, script):
        return dict(self.run_js_result)


class SignupNavigationTests(unittest.TestCase):
    def test_navigation_waits_only_for_dom_content(self):
        page = _SignupPage()
        browser = mock.Mock()
        browser.get_tabs.return_value = [page]
        with (
            mock.patch.object(signup_flow, "active_browser", return_value=browser),
            mock.patch.object(signup_flow, "set_browser_session"),
            mock.patch.object(signup_flow, "sleep_with_cancel"),
            mock.patch.object(signup_flow, "click_email_signup_button") as click_email,
            mock.patch.object(signup_flow, "active_page", return_value=page),
        ):
            signup_flow.open_signup_page()

        self.assertEqual(
            page.calls,
            [
                (
                    signup_flow.SIGNUP_URL,
                    {
                        "wait_until": "domcontentloaded",
                        "timeout": signup_flow.SIGNUP_NAVIGATION_TIMEOUT_MS,
                    },
                )
            ],
        )
        click_email.assert_called_once()

    def test_navigation_timeout_is_soft_when_signup_ui_is_ready(self):
        page = _SignupPage(TimeoutError("Timeout 30000ms exceeded"))
        logs = []
        browser = mock.Mock()
        browser.get_tabs.return_value = [page]
        with (
            mock.patch.object(signup_flow, "active_browser", return_value=browser),
            mock.patch.object(signup_flow, "set_browser_session"),
            mock.patch.object(signup_flow, "sleep_with_cancel"),
            mock.patch.object(signup_flow, "click_email_signup_button"),
            mock.patch.object(signup_flow, "active_page", return_value=page),
            mock.patch.object(signup_flow, "restart_browser") as restart,
        ):
            signup_flow.open_signup_page(log_callback=logs.append)

        restart.assert_not_called()
        self.assertTrue(any("已进入注册域" in message for message in logs))

    def test_proxy_timeout_restarts_browser_when_page_stays_blank(self):
        blank = _SignupPage(
            Exception("Page.goto: NS_ERROR_PROXY_GATEWAY_TIMEOUT"),
            url="about:blank",
        )
        blank.run_js_result = {
            "url": "about:blank",
            "text": "",
            "ready": False,
            "email_form": False,
            "signup_action": False,
        }
        ready = _SignupPage()
        browsers = [
            mock.Mock(get_tabs=mock.Mock(return_value=[blank])),
            mock.Mock(get_tabs=mock.Mock(return_value=[ready])),
        ]
        logs = []

        def active_browser():
            return browsers[0]

        def restart(**kwargs):
            browsers.pop(0)

        with (
            mock.patch.object(signup_flow, "active_browser", side_effect=active_browser),
            mock.patch.object(signup_flow, "set_browser_session"),
            mock.patch.object(signup_flow, "sleep_with_cancel"),
            mock.patch.object(
                signup_flow,
                "_wait_for_signup_page",
                side_effect=lambda page_obj, timeout=12, cancel_callback=None: signup_flow._signup_page_state(page_obj),
            ),
            mock.patch.object(signup_flow, "click_email_signup_button"),
            mock.patch.object(signup_flow, "active_page", return_value=ready),
            mock.patch.object(signup_flow, "restart_browser", side_effect=restart),
            mock.patch.object(signup_flow, "stop_browser"),
        ):
            signup_flow.open_signup_page(log_callback=logs.append)

        self.assertTrue(any("NS_ERROR_PROXY_GATEWAY_TIMEOUT" in message for message in logs))
        self.assertTrue(any("重启浏览器后重试" in message for message in logs))

    def test_skips_email_button_when_email_form_is_already_visible(self):
        page = _SignupPage()
        page.run_js_result["email_form"] = True
        page.run_js_result["signup_action"] = False
        browser = mock.Mock()
        browser.get_tabs.return_value = [page]
        with (
            mock.patch.object(signup_flow, "active_browser", return_value=browser),
            mock.patch.object(signup_flow, "set_browser_session"),
            mock.patch.object(signup_flow, "sleep_with_cancel"),
            mock.patch.object(signup_flow, "click_email_signup_button") as click_email,
            mock.patch.object(signup_flow, "active_page", return_value=page),
        ):
            logs = []
            signup_flow.open_signup_page(log_callback=logs.append)

        click_email.assert_not_called()
        self.assertTrue(any("跳过「使用邮箱注册」按钮" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
