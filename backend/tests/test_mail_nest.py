import time
import unittest
from unittest import mock

from backend.mailbox import mail_nest
from backend.registration import engine as gr


def _response(payload):
    response = mock.Mock()
    response.json.return_value = payload
    response.text = str(payload)
    return response


class MailNestPoolTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(gr.config)
        gr.config["email_provider"] = "mailnest"
        gr.config["mailnest_api_key"] = "test-key"
        mail_nest.reset_pool()

    def tearDown(self):
        mail_nest.reset_pool()
        gr.config.clear()
        gr.config.update(self.original_config)

    def test_buy_order_keeps_expiry(self):
        def http_post(url, **kwargs):
            self.assertIn("/email/temporary/buy", url)
            return _response(
                {
                    "code": "00000",
                    "data": [
                        {
                            "id": "order-1",
                            "email": "fresh@outlook.com",
                            "expired_at": "2026-06-10T12:20:00+08:00",
                        }
                    ],
                }
            )

        order = mail_nest.buy_order(http_post, "test-key", "x-ai001")
        self.assertEqual(order.email, "fresh@outlook.com")
        self.assertEqual(order.order_id, "order-1")
        self.assertGreater(order.expired_at, 0)

    def test_unreceived_mailbox_is_released(self):
        order = mail_nest.MailOrder(
            email="fresh@outlook.com",
            expired_at=time.time() + 1000,
        )
        mail_nest.track_order(order)
        calls = []

        def http_post(url, **kwargs):
            calls.append((url, kwargs.get("json")))
            return _response({"code": "00000", "data": None})

        with mock.patch.object(gr, "http_post", http_post):
            action = gr.settle_mailnest_email(
                "fresh@outlook.com",
                RuntimeError("MailNest 在 60s 内未收到验证码邮件"),
            )
        self.assertEqual(action, "released")
        self.assertEqual(calls[0][1]["email"], "fresh@outlook.com")
        self.assertIsNone(mail_nest.get_order("fresh@outlook.com"))

    def test_rate_limited_mailbox_stays_out_until_cooldown_ends(self):
        order = mail_nest.MailOrder(
            email="limited@outlook.com",
            expired_at=time.time() + 7200,
            code_received=True,
        )
        mail_nest.track_order(order)
        now = time.time()
        action = gr.settle_mailnest_email(
            "limited@outlook.com",
            gr.EmailCodeRateLimited(
                "limited@outlook.com",
                "Too many code requests. Please wait a few minutes before requesting another code.",
            ),
        )
        self.assertEqual(action, "cooled")
        self.assertIsNone(mail_nest.claim_reusable(None, now=now + 60))
        claimed = mail_nest.claim_reusable(
            None,
            now=now + mail_nest.CODE_RATE_COOLDOWN_SECONDS + 5,
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.email, "limited@outlook.com")

    def test_rate_limit_text_also_cools_the_mailbox(self):
        order = mail_nest.MailOrder(
            email="limited-text@outlook.com",
            expired_at=time.time() + 7200,
            code_received=True,
        )
        mail_nest.track_order(order)
        action = gr.settle_mailnest_email(
            "limited-text@outlook.com",
            RuntimeError("Too many code requests. Please wait a few minutes before requesting another code."),
        )
        self.assertEqual(action, "cooled")
        self.assertIsNone(mail_nest.claim_reusable(None))

    def test_rate_limited_mailbox_expiring_inside_cooldown_is_dropped(self):
        order = mail_nest.MailOrder(
            email="expiring@outlook.com",
            expired_at=time.time() + mail_nest.CODE_RATE_COOLDOWN_SECONDS + 60,
            code_received=True,
        )
        mail_nest.track_order(order)
        action = gr.settle_mailnest_email(
            "expiring@outlook.com",
            gr.EmailCodeRateLimited("expiring@outlook.com", "验证码请求过多"),
        )
        self.assertEqual(action, "discarded")
        self.assertIsNone(mail_nest.get_order("expiring@outlook.com"))

    def test_charged_mailbox_is_reused_after_later_failure(self):
        order = mail_nest.MailOrder(
            email="used@outlook.com",
            expired_at=time.time() + 1000,
            code_received=True,
        )
        mail_nest.track_order(order)
        action = gr.settle_mailnest_email(
            "used@outlook.com",
            RuntimeError("最终注册页资料填写失败"),
        )
        self.assertEqual(action, "reused")
        claimed = mail_nest.claim_reusable(None)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.email, "used@outlook.com")

    def test_mailbox_inside_three_minute_margin_is_not_reused(self):
        order = mail_nest.MailOrder(
            email="soon@outlook.com",
            expired_at=time.time() + 60,
            code_received=True,
        )
        mail_nest.track_order(order)
        action = gr.settle_mailnest_email(
            "soon@outlook.com",
            RuntimeError("最终注册页资料填写失败"),
        )
        self.assertEqual(action, "discarded")
        self.assertIsNone(mail_nest.claim_reusable(None))

    def test_sso_timeout_reuses_the_same_mailbox_once(self):
        order = mail_nest.MailOrder(
            email="sso@outlook.com",
            expired_at=time.time() + 1000,
            code_received=True,
        )
        mail_nest.track_order(order)
        exc = RuntimeError("sso_timeout：等待超时未获取到 sso cookie")
        self.assertEqual(gr.settle_mailnest_email("sso@outlook.com", exc), "retry_same")
        claimed = mail_nest.claim_reusable(None)
        self.assertEqual(claimed.email, "sso@outlook.com")
        self.assertEqual(gr.settle_mailnest_email("sso@outlook.com", exc), "discarded")
        self.assertIsNone(mail_nest.get_order("sso@outlook.com"))

    def test_existing_account_is_not_reused(self):
        order = mail_nest.MailOrder(
            email="taken@outlook.com",
            expired_at=time.time() + 1000,
            code_received=True,
        )
        mail_nest.track_order(order)
        action = gr.settle_mailnest_email(
            "taken@outlook.com",
            gr._rf.AccountAlreadyRegistered("账号已注册"),
        )
        self.assertEqual(action, "discarded")
        self.assertIsNone(mail_nest.claim_reusable(None))

    def test_saved_sso_drops_the_mailbox(self):
        order = mail_nest.MailOrder(
            email="done@outlook.com",
            expired_at=time.time() + 1000,
            code_received=True,
        )
        mail_nest.track_order(order)
        action = gr.settle_mailnest_email("done@outlook.com", sso="sso-token")
        self.assertEqual(action, "discarded")
        self.assertIsNone(mail_nest.get_order("done@outlook.com"))

    def test_release_rejected_as_charged_still_reuses(self):
        order = mail_nest.MailOrder(
            email="charged@outlook.com",
            expired_at=time.time() + 1000,
        )
        mail_nest.track_order(order)

        def http_post(url, **kwargs):
            return _response({"code": "D0004", "msg": "当前无法对此邮箱执行该操作"})

        with mock.patch.object(gr, "http_post", http_post):
            action = gr.settle_mailnest_email(
                "charged@outlook.com",
                RuntimeError("MailNest 在 60s 内未收到验证码邮件"),
            )
        self.assertEqual(action, "reused")
        claimed = mail_nest.claim_reusable(None)
        self.assertEqual(claimed.email, "charged@outlook.com")
        self.assertTrue(claimed.code_received)

    def test_second_code_ignores_the_mail_that_was_already_used(self):
        order = mail_nest.MailOrder(
            email="again@outlook.com",
            expired_at=time.time() + 1000,
            code_received=True,
            last_received_at=mail_nest.parse_time("2026-06-10T12:00:00+08:00"),
        )
        order.used_codes.add("111111")
        mail_nest.track_order(order)

        def http_post(url, **kwargs):
            return _response(
                {
                    "code": "00000",
                    "data": [
                        {
                            "id": "old",
                            "subject": "old",
                            "body_preview": "code 111111",
                            "code_match": "111111",
                            "received_at": "2026-06-10T12:00:00+08:00",
                        },
                        {
                            "id": "new",
                            "subject": "new",
                            "body_preview": "code 222222",
                            "code_match": "222222",
                            "received_at": "2026-06-10T12:05:00+08:00",
                        },
                    ],
                }
            )

        code = mail_nest.wait_for_code(
            http_post,
            "test-key",
            "again@outlook.com",
            timeout=5,
            poll_interval=1,
            raise_if_cancelled=lambda callback: None,
            sleep_with_cancel=lambda seconds, callback: None,
        )
        self.assertEqual(code, "222222")
        self.assertIn("222222", mail_nest.get_order("again@outlook.com").used_codes)
