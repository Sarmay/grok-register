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

    def test_naive_expiry_is_beijing_time(self):
        # 真实买号响应：18:20:44 买入，有效至 18:40:44，只有 20 分钟。
        expired_at = mail_nest.parse_time("2026-09-23 18:40:44")
        self.assertEqual(expired_at, mail_nest.parse_time("2026-09-23T18:40:44+08:00"))
        bought_at = mail_nest.parse_time("2026-09-23T18:20:44+08:00")
        self.assertEqual(expired_at - bought_at, 20 * 60)

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
            expired_at=time.time() + 7200,
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
        # MailNest 说已扣费，说明刚有验证码到达：先冷却，再复用。
        self.assertIsNone(mail_nest.claim_reusable(None))
        claimed = mail_nest.claim_reusable(None, now=time.time() + mail_nest.CODE_RATE_COOLDOWN_SECONDS + 5)
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


class MailNestPoolPersistenceTests(unittest.TestCase):
    """订单池挂上 SQLite 后，重启不能丢掉已扣费的邮箱。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from backend.registration.store import RegistrationRepository

        self.tmp = tempfile.TemporaryDirectory()
        self.store = RegistrationRepository(Path(self.tmp.name) / "results.sqlite3")
        self.original_config = dict(gr.config)
        gr.config["email_provider"] = "mailnest"
        gr.config["mailnest_api_key"] = "test-key"
        mail_nest.reset_pool()
        mail_nest.bind_store(self.store)

    def tearDown(self):
        mail_nest.reset_pool()
        gr.config.clear()
        gr.config.update(self.original_config)
        self.tmp.cleanup()

    def _restart(self):
        """模拟进程重启：清空内存，只保留数据库。"""
        mail_nest.reset_pool()
        mail_nest.bind_store(self.store)
        return mail_nest.hydrate()

    def test_charged_mailbox_survives_restart_and_is_reused(self):
        order = mail_nest.MailOrder(email="kept@outlook.com", expired_at=time.time() + 7200)
        mail_nest.track_order(order)
        mail_nest.remember_received_code("kept@outlook.com", "123456", time.time())
        self.assertTrue(mail_nest.recycle_order("kept@outlook.com"))

        rows = self.store.list_mailnest_orders()
        self.assertEqual([row["email"] for row in rows], ["kept@outlook.com"])
        self.assertEqual(rows[0]["state"], mail_nest.STATE_AVAILABLE)
        self.assertEqual(rows[0]["used_codes"], ["123456"])

        result = self._restart()
        self.assertEqual([item.email for item in result.restored], ["kept@outlook.com"])
        self.assertEqual(result.stale, [])
        # 刚收过验证码，冷却结束前不会被取走；冷却结束后继续复用。
        self.assertIsNone(mail_nest.claim_reusable(None))
        claimed = mail_nest.claim_reusable(None, now=time.time() + mail_nest.CODE_RATE_COOLDOWN_SECONDS + 5)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.email, "kept@outlook.com")
        self.assertIn("123456", claimed.used_codes)
        self.assertEqual(self.store.list_mailnest_orders()[0]["state"], mail_nest.STATE_INFLIGHT)

    def test_expiry_saved_as_utc_is_recomputed_on_restart(self):
        # 旧版本把 "18:40:44" 当 UTC 存下，时间戳多出 8 小时，重启后看起来还能用很久。
        text = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600 - 600))
        order = mail_nest.MailOrder(
            email="stale@outlook.com",
            expired_at=time.time() + 8 * 3600 - 600,
            expired_at_text=text,
            code_received=True,
            last_code_at=time.time() - mail_nest.CODE_RATE_COOLDOWN_SECONDS - 10,
        )
        mail_nest.track_order(order)
        mail_nest.recycle_order("stale@outlook.com")

        result = self._restart()
        self.assertEqual(result.restored, [])
        self.assertEqual(self.store.list_mailnest_orders(), [])

    def test_dropped_mailbox_is_removed_from_storage(self):
        mail_nest.track_order(mail_nest.MailOrder(email="gone@outlook.com", expired_at=time.time() + 7200))
        self.assertEqual(len(self.store.list_mailnest_orders()), 1)
        mail_nest.drop_order("gone@outlook.com")
        self.assertEqual(self.store.list_mailnest_orders(), [])
        self.assertEqual(self._restart().restored, [])

    def test_expired_mailbox_is_not_restored(self):
        order = mail_nest.MailOrder(email="old@outlook.com", expired_at=time.time() + 60, code_received=True)
        mail_nest.track_order(order)
        mail_nest.recycle_order("old@outlook.com")
        # 落盘时还有余量，但重启后已经不够 3 分钟。
        result = self._restart()
        self.assertEqual(result.restored, [])
        self.assertEqual(self.store.list_mailnest_orders(), [])

    def test_unfinished_uncharged_mailbox_is_released_on_startup(self):
        mail_nest.track_order(mail_nest.MailOrder(email="frozen@outlook.com", expired_at=time.time() + 7200))
        mail_nest.reset_pool()
        calls = []

        def http_post(url, **kwargs):
            calls.append((url, kwargs.get("json")))
            return _response({"code": "00000", "data": None})

        logs = []
        with (
            mock.patch.object(gr, "get_registration_repository", return_value=self.store),
            mock.patch.object(gr, "http_post", http_post),
        ):
            gr.prepare_mailnest_pool(logs.append)
            # 第二次调用不能再释放一遍。
            gr.prepare_mailnest_pool(logs.append)

        self.assertEqual(len(calls), 1)
        self.assertIn("/email/release", calls[0][0])
        self.assertEqual(calls[0][1], {"email": "frozen@outlook.com"})
        self.assertIsNone(mail_nest.get_order("frozen@outlook.com"))
        self.assertEqual(self.store.list_mailnest_orders(), [])
        self.assertTrue(any("退回冻结" in line for line in logs))

    def test_startup_release_that_turns_out_charged_keeps_mailbox(self):
        mail_nest.track_order(mail_nest.MailOrder(email="paid@outlook.com", expired_at=time.time() + 7200))
        mail_nest.reset_pool()

        def http_post(url, **kwargs):
            return _response({"code": "D0004", "msg": "已扣费"})

        with (
            mock.patch.object(gr, "get_registration_repository", return_value=self.store),
            mock.patch.object(gr, "http_post", http_post),
        ):
            gr.prepare_mailnest_pool()

        rows = self.store.list_mailnest_orders()
        self.assertEqual([row["email"] for row in rows], ["paid@outlook.com"])
        self.assertEqual(rows[0]["state"], mail_nest.STATE_AVAILABLE)
        self.assertTrue(rows[0]["code_received"])
        self.assertIsNone(mail_nest.claim_reusable(None))
        later = time.time() + mail_nest.CODE_RATE_COOLDOWN_SECONDS + 5
        self.assertEqual(mail_nest.claim_reusable(None, now=later).email, "paid@outlook.com")

    def test_storage_failure_does_not_break_the_pool(self):
        broken = mock.Mock()
        broken.save_mailnest_order.side_effect = RuntimeError("disk full")
        broken.delete_mailnest_order.side_effect = RuntimeError("disk full")
        broken.list_mailnest_orders.side_effect = RuntimeError("disk full")
        mail_nest.reset_pool()
        mail_nest.bind_store(broken)
        self.assertEqual(mail_nest.hydrate().restored, [])
        order = mail_nest.MailOrder(email="memory@outlook.com", expired_at=time.time() + 7200, code_received=True)
        mail_nest.track_order(order)
        self.assertTrue(mail_nest.recycle_order("memory@outlook.com"))
        self.assertEqual(mail_nest.claim_reusable(None).email, "memory@outlook.com")


class MailNestReuseCooldownTests(unittest.TestCase):
    """刚收过验证码的邮箱要等满冷却再复用，否则下一次索码几乎必定被 xAI 限流。"""

    def setUp(self):
        self.original_config = dict(gr.config)
        gr.config["email_provider"] = "mailnest"
        gr.config["mailnest_api_key"] = "test-key"
        mail_nest.reset_pool()

    def tearDown(self):
        mail_nest.reset_pool()
        gr.config.clear()
        gr.config.update(self.original_config)

    def test_fresh_code_puts_mailbox_on_cooldown(self):
        now = time.time()
        order = mail_nest.MailOrder(email="fresh@outlook.com", expired_at=now + 7200)
        mail_nest.track_order(order)
        mail_nest.remember_received_code("fresh@outlook.com", "654321", now)
        action = gr.settle_mailnest_email("fresh@outlook.com", RuntimeError("最终注册页资料填写失败"))
        self.assertEqual(action, "reused")
        self.assertIsNone(mail_nest.claim_reusable(None, now=now + 300))
        claimed = mail_nest.claim_reusable(None, now=now + mail_nest.CODE_RATE_COOLDOWN_SECONDS + 1)
        self.assertEqual(claimed.email, "fresh@outlook.com")

    def test_cooldown_longer_than_remaining_life_drops_mailbox(self):
        now = time.time()
        order = mail_nest.MailOrder(
            email="short@outlook.com",
            expired_at=now + mail_nest.CODE_RATE_COOLDOWN_SECONDS + 60,
        )
        mail_nest.track_order(order)
        mail_nest.remember_received_code("short@outlook.com", "111222", now)
        action = gr.settle_mailnest_email("short@outlook.com", RuntimeError("最终注册页资料填写失败"))
        self.assertEqual(action, "discarded")
        self.assertIsNone(mail_nest.get_order("short@outlook.com"))

    def test_old_code_does_not_block_reuse(self):
        now = time.time()
        order = mail_nest.MailOrder(
            email="rested@outlook.com",
            expired_at=now + 7200,
            code_received=True,
            last_code_at=now - mail_nest.CODE_RATE_COOLDOWN_SECONDS - 10,
        )
        mail_nest.track_order(order)
        self.assertTrue(mail_nest.recycle_order("rested@outlook.com"))
        self.assertEqual(mail_nest.claim_reusable(None, now=now).email, "rested@outlook.com")
