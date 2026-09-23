import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.registration import engine
from backend.registration.store import RegistrationRepository
from backend.web.jobs import RegistrationJobCoordinator
from backend.web.relogin_jobs import ReloginJobCoordinator
from backend.web.sso_check_jobs import SsoCheckJobCoordinator
from backend.web.task_history import (
    KIND_REGISTRATION,
    KIND_RELOGIN,
    KIND_SSO_CHECK,
    TaskRunRecorder,
    prune_history,
    retention_settings,
)


def _wait_idle(coordinator, timeout=3.0):
    """等工作线程真正退出：历史落盘发生在 running 置回 False 之后，只看状态会抢跑。"""
    thread = coordinator._thread
    if thread is not None:
        thread.join(timeout)
    return not coordinator.status()["running"] and (thread is None or not thread.is_alive())


class TaskRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RegistrationRepository(Path(self.tmp.name) / "results.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_lifecycle_keeps_logs_with_full_timestamps(self):
        started = time.time() - 30
        self.store.upsert_task_run(KIND_RELOGIN, "run-1", started_at=started, status="running", summary={"total_count": 2})
        self.store.append_task_logs(
            KIND_RELOGIN,
            "run-1",
            [(1, "2026-09-23 09:00:01+08:00", "第一行"), (2, "2026-09-23 09:00:02+08:00", "第二行")],
        )
        self.store.append_task_logs(KIND_RELOGIN, "run-1", [(3, "2026-09-23 09:00:03+08:00", "第三行")])
        self.store.upsert_task_run(
            KIND_RELOGIN, "run-1", finished_at=started + 20, status="finished", summary={"total_count": 2, "success_count": 2}
        )

        run = self.store.get_task_run(KIND_RELOGIN, "run-1")
        self.assertEqual(run["status"], "finished")
        self.assertAlmostEqual(run["started_at"], started, places=3)  # finish 时不覆盖开始时间
        self.assertEqual(run["summary"]["success_count"], 2)
        self.assertEqual(run["log_count"], 3)
        logs = self.store.get_task_logs(KIND_RELOGIN, "run-1")
        self.assertEqual([item["message"] for item in logs], ["第一行", "第二行", "第三行"])
        self.assertEqual(logs[0]["time"], "09:00:01")
        self.assertEqual(logs[0]["timestamp"], "2026-09-23 09:00:01+08:00")
        self.assertEqual([item["id"] for item in self.store.get_task_logs(KIND_RELOGIN, "run-1", after_seq=2)], [3])

    def test_registration_history_includes_batches_without_saved_logs(self):
        self.store.add_result({"email": "old-a@example.com", "status": "success", "batch_id": "web-old",
                               "started_at": "2026-09-18 10:00:00", "finished_at": "2026-09-18 10:05:00"})
        self.store.add_result({"email": "old-b@example.com", "status": "failure", "batch_id": "web-old",
                               "started_at": "2026-09-18 10:06:00", "finished_at": "2026-09-18 10:09:00"})
        self.store.add_result({"email": "new@example.com", "status": "success", "batch_id": "web-new"})
        self.store.upsert_task_run(KIND_REGISTRATION, "web-new", started_at=time.time(), status="finished",
                                   summary={"target_count": 1})
        self.store.append_task_logs(KIND_REGISTRATION, "web-new", [(1, "2026-09-23 09:00:00+08:00", "开始")])

        items, total = self.store.list_task_runs(KIND_REGISTRATION)
        self.assertEqual(total, 2)
        self.assertEqual([item["run_id"] for item in items], ["web-new", "web-old"])
        legacy = items[1]
        self.assertEqual(legacy["counts"], {"total": 2, "success": 1, "failure": 1, "cancelled": 0, "skipped": 0})
        self.assertEqual(legacy["log_count"], 0)
        self.assertEqual(legacy["status"], "finished")
        self.assertIsNotNone(legacy["started_at"])
        self.assertEqual(items[0]["log_count"], 1)
        self.assertNotIn("search_text", items[0])

        matched, count = self.store.list_task_runs(KIND_REGISTRATION, keyword="old-b@")
        self.assertEqual(count, 1)
        self.assertEqual(matched[0]["run_id"], "web-old")
        self.assertEqual(self.store.get_task_run(KIND_REGISTRATION, "web-old")["counts"]["total"], 2)
        self.assertIsNone(self.store.get_task_run(KIND_REGISTRATION, "missing"))

    def test_prune_by_count_and_age_but_never_the_running_run(self):
        now = time.time()
        for index in range(5):
            run_id = f"run-{index}"
            self.store.upsert_task_run(KIND_SSO_CHECK, run_id, started_at=now - index * 60, status="finished")
            self.store.append_task_logs(KIND_SSO_CHECK, run_id, [(1, "", "x")])
        self.store.upsert_task_run(KIND_SSO_CHECK, "ancient", started_at=now - 90 * 86400, status="finished")
        self.store.upsert_task_run(KIND_SSO_CHECK, "live", started_at=now - 100 * 86400, status="running")

        removed = self.store.prune_task_runs(KIND_SSO_CHECK, keep_days=60, keep_count=3)
        self.assertEqual(removed, 3)  # run-3、run-4 超出条数，ancient 超龄
        remaining = {item["run_id"] for item in self.store.list_task_runs(KIND_SSO_CHECK, limit=50)[0]}
        self.assertEqual(remaining, {"run-0", "run-1", "run-2", "live"})
        self.assertEqual(self.store.count_task_logs(KIND_SSO_CHECK, "run-4"), 0)
        self.assertEqual(self.store.prune_task_runs(KIND_SSO_CHECK, keep_days=0, keep_count=0), 0)

    def test_delete_clear_interrupt_and_import(self):
        self.store.upsert_task_run(KIND_RELOGIN, "a", started_at=time.time(), status="running", summary={})
        self.store.upsert_task_run(KIND_RELOGIN, "b", started_at=time.time(), status="finished", summary={})
        self.store.append_task_logs(KIND_RELOGIN, "b", [(1, "", "b-log")])
        self.assertTrue(self.store.mark_task_run_interrupted(KIND_RELOGIN, "a", error="服务重启"))
        self.assertFalse(self.store.mark_task_run_interrupted(KIND_RELOGIN, "b"))
        run_a = self.store.get_task_run(KIND_RELOGIN, "a")
        self.assertEqual(run_a["status"], "interrupted")
        self.assertEqual(run_a["summary"]["last_error"], "服务重启")
        self.assertIsNotNone(run_a["finished_at"])

        self.assertTrue(self.store.delete_task_run(KIND_RELOGIN, "b"))
        self.assertFalse(self.store.delete_task_run(KIND_RELOGIN, "b"))
        self.assertEqual(self.store.count_task_logs(KIND_RELOGIN, "b"), 0)

        imported = self.store.import_task_runs(
            KIND_RELOGIN,
            [
                {"run_id": "a", "finished_at": 1.0, "summary": {"total_count": 9}},  # 已存在，不覆盖
                {"run_id": "legacy", "finished_at": 1700000000.0, "total_count": 3, "items": []},
                {"run_id": ""},
                "junk",
            ],
        )
        self.assertEqual(imported, 1)
        self.assertEqual(self.store.get_task_run(KIND_RELOGIN, "a")["summary"].get("total_count"), None)
        legacy = self.store.get_task_run(KIND_RELOGIN, "legacy")
        self.assertEqual(legacy["summary"]["total_count"], 3)
        self.assertEqual(legacy["started_at"], 1700000000.0)

        self.assertEqual(self.store.clear_task_runs(KIND_RELOGIN, keep_run_id="a"), 1)
        self.assertIsNotNone(self.store.get_task_run(KIND_RELOGIN, "a"))
        with self.assertRaises(ValueError):
            self.store.list_task_runs("bogus")


class TaskRunRecorderTests(unittest.TestCase):
    class _Repo:
        def __init__(self):
            self.runs = []
            self.batches = []

        def upsert_task_run(self, kind, run_id, **kwargs):
            self.runs.append((kind, run_id, kwargs))

        def append_task_logs(self, kind, run_id, rows):
            self.batches.append(list(rows))

    def test_flushes_first_line_immediately_then_batches_until_finish(self):
        repo = self._Repo()
        recorder = TaskRunRecorder(KIND_REGISTRATION, "web-1", lambda: repo)
        recorder.begin(100.0, {"target_count": 2})
        recorder.append(1, "第一行", "2026-09-23 09:00:00+08:00")
        recorder.append(2, "第二行")
        recorder.append(3, "第三行")
        self.assertEqual(len(repo.batches), 1)  # 第一行立刻写，后面两行在攒
        recorder.finish(160.0, "finished", {"target_count": 2, "success_count": 2})
        self.assertEqual([len(batch) for batch in repo.batches], [1, 2])
        self.assertEqual([row[0] for batch in repo.batches for row in batch], [1, 2, 3])
        self.assertTrue(repo.batches[1][0][1].endswith(("+08:00", "+00:00", "Z")) or "T" in repo.batches[1][0][1] or " " in repo.batches[1][0][1])
        self.assertEqual(repo.runs[0][2]["status"], "running")
        self.assertEqual(repo.runs[-1][2]["status"], "finished")
        self.assertEqual(repo.runs[-1][2]["finished_at"], 160.0)

    def test_repository_errors_never_escape(self):
        broken = mock.Mock()
        broken.upsert_task_run.side_effect = RuntimeError("locked")
        broken.append_task_logs.side_effect = RuntimeError("locked")
        recorder = TaskRunRecorder(KIND_RELOGIN, "r", lambda: broken)
        recorder.begin(1.0, {})
        recorder.append(1, "x")
        recorder.finish(2.0, "finished", {})
        missing = TaskRunRecorder(KIND_RELOGIN, "r", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
        missing.append(1, "x")
        missing.finish(2.0, "finished", {})

    def test_retention_settings_and_prune_helper(self):
        self.assertEqual(retention_settings({}), (60, 200))
        self.assertEqual(retention_settings({"task_history_retention_days": "7", "task_history_retention_count": 0}), (7, 0))
        self.assertEqual(retention_settings({"task_history_retention_days": "bad"}), (60, 200))
        repo = mock.Mock()
        repo.prune_task_runs.side_effect = [2, RuntimeError("x"), 1]
        self.assertEqual(prune_history(repo, {"task_history_retention_days": 1}), 3)
        self.assertEqual(prune_history(None, {}), 0)


class RegistrationJobHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RegistrationRepository(Path(self.tmp.name) / "results.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_logs_are_saved_under_the_batch_id_from_the_first_line(self):
        coordinator = RegistrationJobCoordinator()
        seen = {}

        def fake_run(count):
            seen["batch_id"] = engine.new_registration_batch_id("web")
            engine.registration_log("[*] 1. 打开注册页")
            engine.registration_log("[+] 注册成功: someone@example.com")

        with (
            mock.patch.object(coordinator, "_repository", return_value=self.store),
            mock.patch.object(engine, "run_registration", side_effect=fake_run),
            mock.patch.object(engine, "_wire_runtime_modules"),
            mock.patch.object(engine._bs, "allow_browser_launches"),
        ):
            status = coordinator.start(count=1, workers=1)
            batch_id = status["batch_id"]
            self.assertTrue(batch_id.startswith("web-"))
            self.assertTrue(_wait_idle(coordinator))

        self.assertEqual(seen["batch_id"], batch_id)
        run = self.store.get_task_run(KIND_REGISTRATION, batch_id)
        self.assertEqual(run["status"], "finished")
        self.assertEqual(run["summary"]["success_count"], 1)
        self.assertEqual(run["summary"]["target_count"], 1)
        messages = [item["message"] for item in self.store.get_task_logs(KIND_REGISTRATION, batch_id)]
        self.assertEqual(messages[0], "[*] Web 任务启动：数量=1 并发=1")
        self.assertIn(f"[*] 任务批次: {batch_id}", messages)
        self.assertIn("[+] 注册成功: someone@example.com", messages)
        self.assertEqual(messages[-1], "[*] Web 任务已结束")
        first = self.store.get_task_logs(KIND_REGISTRATION, batch_id)[0]
        self.assertRegex(first["timestamp"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertEqual(coordinator.get_logs()[0]["timestamp"], first["timestamp"])

    def test_engine_exception_marks_run_failed_and_restart_marks_interrupted(self):
        coordinator = RegistrationJobCoordinator()
        with (
            mock.patch.object(coordinator, "_repository", return_value=self.store),
            mock.patch.object(engine, "run_registration", side_effect=RuntimeError("boom")),
            mock.patch.object(engine, "_wire_runtime_modules"),
            mock.patch.object(engine._bs, "allow_browser_launches"),
        ):
            batch_id = coordinator.start(count=2, workers=1)["batch_id"]
            self.assertTrue(_wait_idle(coordinator))
        run = self.store.get_task_run(KIND_REGISTRATION, batch_id)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["summary"]["last_error"], "boom")

        # 模拟服务重启：快照仍是 running，历史里的这条也还挂着 running。
        self.store.upsert_task_run(KIND_REGISTRATION, "web-crashed", started_at=time.time(), status="running")
        self.store.save_job_snapshot({"batch_id": "web-crashed", "running": True, "target_count": 3})
        fresh = RegistrationJobCoordinator()
        with mock.patch.object(fresh, "_repository", return_value=self.store):
            fresh.restore_from_database()
        crashed = self.store.get_task_run(KIND_REGISTRATION, "web-crashed")
        self.assertEqual(crashed["status"], "interrupted")
        self.assertIn("服务重启", crashed["summary"]["last_error"])


class ReloginAndSsoHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RegistrationRepository(Path(self.tmp.name) / "results.sqlite3")
        self.first = self.store.add_result({"email": "one@example.com", "password": "pw", "status": "success"})
        self.second = self.store.add_result({"email": "two@example.com", "password": "pw", "status": "success"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_relogin_run_summary_and_logs_survive_in_store(self):
        coordinator = ReloginJobCoordinator()

        def run_record(record, _store):
            return {"error": "" if record["id"] == self.first else "登录失败"}

        with (
            mock.patch.object(engine, "get_registration_repository", return_value=self.store),
            mock.patch.object(coordinator, "_run_record", side_effect=run_record),
        ):
            run_id = coordinator.start_many([self.first, self.second])["run_id"]
            self.assertTrue(_wait_idle(coordinator))

        run = self.store.get_task_run(KIND_RELOGIN, run_id)
        self.assertEqual(run["status"], "finished")
        self.assertEqual(run["summary"]["success_count"], 1)
        self.assertEqual(run["summary"]["failed_count"], 1)
        self.assertEqual([item["email"] for item in run["summary"]["items"]], ["one@example.com", "two@example.com"])
        self.assertNotIn("log_count", run["summary"])
        messages = [item["message"] for item in self.store.get_task_logs(KIND_RELOGIN, run_id)]
        self.assertIn("[*] 重新登录任务启动：共 2 个账号，可执行 2 个", messages)
        self.assertIn("[!] two@example.com: 登录失败", messages)
        self.assertEqual(messages[-1], "[*] 重新登录任务已结束")
        listed, total = self.store.list_task_runs(KIND_RELOGIN, keyword="two@example")
        self.assertEqual((total, listed[0]["run_id"]), (1, run_id))

    def test_sso_check_run_is_recorded_with_per_account_lines(self):
        coordinator = SsoCheckJobCoordinator()
        outcomes = {
            self.first: {"status": "clean", "verdict": "clean", "bot_flag_source": 0, "error": ""},
            self.second: {"status": "failed", "verdict": "error", "bot_flag_source": None, "error": "网络错误"},
        }
        with (
            mock.patch.object(engine, "get_registration_repository", return_value=self.store),
            mock.patch.object(coordinator, "_find_sso_file", return_value=Path(__file__)),
            mock.patch.object(coordinator, "_run_record", side_effect=lambda record, _store: outcomes[record["id"]]),
        ):
            run_id = coordinator.start_many([self.first, self.second])["run_id"]
            self.assertTrue(_wait_idle(coordinator))

        run = self.store.get_task_run(KIND_SSO_CHECK, run_id)
        self.assertEqual(run["status"], "finished")
        self.assertEqual(run["summary"]["clean_count"], 1)
        self.assertEqual(run["summary"]["failed_count"], 1)
        messages = [item["message"] for item in self.store.get_task_logs(KIND_SSO_CHECK, run_id)]
        self.assertIn("[*] one@example.com: 正常 botFlagSource=0", messages)
        self.assertIn("[!] two@example.com: 检查失败: 网络错误", messages)
        self.assertTrue(messages[-1].startswith("[*] 检查完成"))


if __name__ == "__main__":
    unittest.main()
