# -*- coding: utf-8 -*-
"""任务运行历史。

注册批次、重新登录、SSO 检查三类任务共用同一套落盘方式：任务开始时登记一条
task_runs，运行中的每行日志按 run_id 追加到 task_logs，结束时写回摘要和状态。
内存里的环形日志只服务实时页面；历史页面读的是这里写进 SQLite 的内容。
"""
from __future__ import annotations

import datetime
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from backend.registration.store import (
    TASK_KIND_REGISTRATION as KIND_REGISTRATION,
    TASK_KIND_RELOGIN as KIND_RELOGIN,
    TASK_KIND_SSO_CHECK as KIND_SSO_CHECK,
    TASK_KINDS,
)

__all__ = [
    "KIND_REGISTRATION",
    "KIND_RELOGIN",
    "KIND_SSO_CHECK",
    "TASK_KINDS",
    "TaskRunRecorder",
    "now_iso",
    "prune_history",
    "retention_settings",
]

DEFAULT_RETENTION_DAYS = 60
DEFAULT_RETENTION_COUNT = 200
# 攒够这么多行或隔这么久就写一批；任务结束时 finish() 会把剩余的全部写完。
FLUSH_LINES = 50
FLUSH_SECONDS = 1.0


def now_iso() -> str:
    """带日期和时区偏移的本地时间，例如 2026-09-23 09:15:02+08:00。"""
    return datetime.datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")


def retention_settings(config: Optional[Mapping[str, Any]]) -> Tuple[int, int]:
    """返回 (保留天数, 保留条数)，0 表示该维度不限制。"""
    source = config or {}

    def _read(key: str, default: int) -> int:
        try:
            return max(int(source.get(key, default) or 0), 0)
        except (TypeError, ValueError):
            return default

    return (
        _read("task_history_retention_days", DEFAULT_RETENTION_DAYS),
        _read("task_history_retention_count", DEFAULT_RETENTION_COUNT),
    )


def prune_history(repository: Any, config: Optional[Mapping[str, Any]], kind: str = "") -> int:
    """按配置清理旧任务；任何异常都吞掉，清理失败不能挡住新任务。"""
    if repository is None:
        return 0
    days, count = retention_settings(config)
    kinds = [kind] if kind else list(TASK_KINDS)
    removed = 0
    for item in kinds:
        try:
            removed += int(repository.prune_task_runs(item, keep_days=days, keep_count=count) or 0)
        except Exception:
            continue
    return removed


class TaskRunRecorder:
    """一次任务的落盘器。

    仓储通过 getter 惰性获取，拿不到或写失败都静默跳过：历史是附属功能，
    不能因为数据库暂时不可写就让注册任务失败。
    """

    def __init__(self, kind: str, run_id: str, repository_getter: Callable[[], Any]) -> None:
        self.kind = str(kind)
        self.run_id = str(run_id or "").strip()
        self._repository_getter = repository_getter
        self._lock = threading.Lock()
        self._pending: List[Tuple[int, str, str]] = []
        self._last_flush = 0.0

    def _repository(self) -> Any:
        try:
            return self._repository_getter()
        except Exception:
            return None

    def begin(self, started_at: Optional[float], summary: Optional[Dict[str, Any]] = None) -> None:
        repository = self._repository()
        if repository is None or not self.run_id:
            return
        try:
            repository.upsert_task_run(
                self.kind,
                self.run_id,
                started_at=started_at or time.time(),
                finished_at=None,
                status="running",
                summary=dict(summary or {}),
            )
        except Exception:
            pass

    def append(self, seq: int, message: str, logged_at: str = "") -> None:
        if not self.run_id:
            return
        with self._lock:
            self._pending.append((int(seq), logged_at or now_iso(), str(message or "")))
            due = (
                len(self._pending) >= FLUSH_LINES
                or time.monotonic() - self._last_flush >= FLUSH_SECONDS
            )
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            batch, self._pending = self._pending, []
            self._last_flush = time.monotonic()
        if not batch:
            return
        repository = self._repository()
        if repository is None:
            return
        try:
            repository.append_task_logs(self.kind, self.run_id, batch)
        except Exception:
            pass

    def finish(
        self,
        finished_at: Optional[float],
        status: str,
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.flush()
        repository = self._repository()
        if repository is None or not self.run_id:
            return
        try:
            repository.upsert_task_run(
                self.kind,
                self.run_id,
                started_at=None,
                finished_at=finished_at or time.time(),
                status=str(status or "finished"),
                summary=dict(summary or {}),
            )
        except Exception:
            pass
