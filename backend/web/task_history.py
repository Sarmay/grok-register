# -*- coding: utf-8 -*-
"""任务运行历史。

注册批次、重新登录、SSO 检查三类任务共用同一套落盘方式：任务开始时登记一条
task_runs，运行中的每行日志按 run_id 追加到 task_logs，结束时写回摘要和状态。
内存里的环形日志只服务实时页面；历史页面读的是这里写进 SQLite 的内容。

SQLite 是攒批写入的，另外每行日志还会立即追加到 logs/tasks/<类型>-<run_id>.log，
方便直接 tail 查看正在跑的任务。文件和数据库记录按同一套保留策略清理。
"""
from __future__ import annotations

import datetime
import os
import re
import threading
import time
from pathlib import Path
from typing import IO, Any, Callable, Dict, List, Mapping, Optional, Tuple

from backend.shared.paths import PROJECT_ROOT

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
    "clear_log_files",
    "delete_log_file",
    "log_file_path",
    "now_iso",
    "prune_history",
    "retention_settings",
]

DEFAULT_RETENTION_DAYS = 60
DEFAULT_RETENTION_COUNT = 200
# 攒够这么多行或隔这么久就写一批；任务结束时 finish() 会把剩余的全部写完。
FLUSH_LINES = 50
FLUSH_SECONDS = 1.0
TASK_LOG_DIR = Path(os.environ.get("GROK_LOG_DIR") or PROJECT_ROOT / "logs") / "tasks"
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.@+-]+")


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


def log_file_path(kind: str, run_id: str) -> Path:
    name = _UNSAFE_NAME_CHARS.sub("_", f"{kind}-{run_id}").strip("._") or str(kind)
    return TASK_LOG_DIR / f"{name}.log"


def delete_log_file(kind: str, run_id: str) -> None:
    try:
        log_file_path(kind, run_id).unlink(missing_ok=True)
    except OSError:
        pass


def _log_files(kind: str) -> List[Path]:
    """该类任务的日志文件，最新修改的排在前面。"""
    try:
        files = [path for path in TASK_LOG_DIR.glob(f"{kind}-*.log") if path.is_file()]
        return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return []


def clear_log_files(kind: str, keep_run_id: str = "") -> None:
    keep = log_file_path(kind, keep_run_id) if keep_run_id else None
    for path in _log_files(kind):
        if path != keep:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _prune_log_files(kind: str, days: int, count: int) -> None:
    """按修改时间套用保留策略。运行中的任务一直在写，总是最新的，不会被清掉。"""
    cutoff = time.time() - days * 86400 if days else None
    for index, path in enumerate(_log_files(kind)):
        try:
            if (count and index >= count) or (cutoff is not None and path.stat().st_mtime < cutoff):
                path.unlink(missing_ok=True)
        except OSError:
            continue


def prune_history(repository: Any, config: Optional[Mapping[str, Any]], kind: str = "") -> int:
    """按配置清理旧任务；任何异常都吞掉，清理失败不能挡住新任务。"""
    days, count = retention_settings(config)
    kinds = [kind] if kind else list(TASK_KINDS)
    for item in kinds:
        _prune_log_files(item, days, count)
    if repository is None:
        return 0
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
    不能因为数据库暂时不可写就让注册任务失败。日志文件同理。
    """

    def __init__(self, kind: str, run_id: str, repository_getter: Callable[[], Any]) -> None:
        self.kind = str(kind)
        self.run_id = str(run_id or "").strip()
        self._repository_getter = repository_getter
        self._lock = threading.Lock()
        self._pending: List[Tuple[int, str, str]] = []
        self._last_flush = 0.0
        self._log_file: Optional[IO[str]] = None
        self._log_file_failed = False

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

    def _write_log_line(self, logged_at: str, message: str) -> None:
        """调用方持有 self._lock。每行立即 flush，tail -f 能实时看到。"""
        if self._log_file is None and not self._log_file_failed:
            try:
                path = log_file_path(self.kind, self.run_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                self._log_file = path.open("a", encoding="utf-8")
            except OSError:
                self._log_file_failed = True
        if self._log_file is None:
            return
        try:
            self._log_file.write(f"[{logged_at}] {message}\n")
            self._log_file.flush()
        except (OSError, ValueError):
            pass

    def _close_log_file(self) -> None:
        with self._lock:
            handle, self._log_file = self._log_file, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def append(self, seq: int, message: str, logged_at: str = "") -> None:
        if not self.run_id:
            return
        stamp = logged_at or now_iso()
        text = str(message or "")
        with self._lock:
            self._write_log_line(stamp, text)
            self._pending.append((int(seq), stamp, text))
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
        self._close_log_file()
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
