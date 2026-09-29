"""MailNest 邮箱渠道适配器。

临时邮箱购买时只冻结金额，大约 20 分钟内有效。第一次成功收件才扣费。
没收到验证码时调用释放接口，把冻结金额退回并换邮箱。
已经收到验证码但注册没成功，并且离过期还超过 3 分钟，只再给一轮。
这一轮再失败，包括没收到新验证码或验证码被限流，就丢掉。

订单池默认只在内存里。bind_store() 挂上仓储后，每次变更都会落盘；
进程重启后 hydrate() 能把还没用完这一轮的已扣费邮箱找回来。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional

from backend.mailbox.utilities import extract_verification_code

API_BASE = "https://mailnest.top"
DEFAULT_PROJECT_CODE = "x-ai001"
REUSE_MARGIN_SECONDS = 180
ALREADY_CHARGED = "D0004"
# MailNest 返回的时间不带时区，实际是北京时间，例如买号时的 "2026-09-23 18:40:44"。
API_TIMEZONE = timezone(timedelta(hours=8))


class ReceiveUnavailableError(Exception):
    """MailNest 当前无法对该邮箱执行收信操作。"""

    def __init__(self, email: str, message: str = ""):
        self.email = str(email or "").strip()
        detail = message or "MailNest 当前无法对此邮箱执行收信操作"
        if self.email and self.email not in detail:
            detail = f"{detail}: {self.email}"
        super().__init__(detail)

STATE_INFLIGHT = "inflight"
STATE_AVAILABLE = "available"

HttpPost = Callable[..., Any]
BlockedEmail = Callable[[str], bool]


@dataclass
class MailOrder:
    email: str
    order_id: str = ""
    expired_at: float = 0.0
    expired_at_text: str = ""
    code_received: bool = False
    # 收到验证码后只再给一轮。置位表示这一轮已经发出，再失败就丢掉。
    retry_used: bool = False
    sso_timeout_reused: bool = False
    used_codes: set[str] = field(default_factory=set)
    # MailNest 返回的收件时间，只用来跳过已经用过的邮件。
    last_received_at: float = 0.0
    # 本机时钟：最近一次拿到验证码或被 MailNest 判定扣费的时间。
    last_code_at: float = 0.0
    code_rate_limited_until: float = 0.0

    def remaining_seconds(self, now: Optional[float] = None) -> int:
        if not self.expired_at:
            return 0
        return int(self.expired_at - (time.time() if now is None else now))

    def reusable(self, now: Optional[float] = None, margin: int = REUSE_MARGIN_SECONDS) -> bool:
        if not self.code_received or not self.expired_at:
            return False
        current = time.time() if now is None else now
        return self.expired_at - current > margin

    def to_payload(self, state: str) -> dict:
        return {
            "email": self.email,
            "order_id": self.order_id,
            "state": state,
            "expired_at": float(self.expired_at or 0.0),
            "expired_at_text": self.expired_at_text,
            "code_received": bool(self.code_received),
            "retry_used": bool(self.retry_used),
            "sso_timeout_reused": bool(self.sso_timeout_reused),
            "used_codes": sorted(self.used_codes),
            "last_received_at": float(self.last_received_at or 0.0),
            "last_code_at": float(self.last_code_at or 0.0),
            "code_rate_limited_until": float(self.code_rate_limited_until or 0.0),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "MailOrder":
        codes = payload.get("used_codes") or []
        if isinstance(codes, str):
            try:
                codes = json.loads(codes or "[]")
            except ValueError:
                codes = []
        expired_at_text = str(payload.get("expired_at_text") or "")
        # 以原始文本为准重新计算，旧版本按 UTC 解析存下的时间戳会多出 8 小时。
        expired_at = parse_time(expired_at_text) or float(payload.get("expired_at") or 0.0)
        return cls(
            email=str(payload.get("email") or "").strip(),
            order_id=str(payload.get("order_id") or ""),
            expired_at=expired_at,
            expired_at_text=expired_at_text,
            code_received=bool(payload.get("code_received")),
            retry_used=bool(payload.get("retry_used") or payload.get("sso_timeout_reused")),
            sso_timeout_reused=bool(payload.get("sso_timeout_reused")),
            used_codes={str(item) for item in codes if str(item)},
            last_received_at=float(payload.get("last_received_at") or 0.0),
            last_code_at=float(payload.get("last_code_at") or 0.0),
            code_rate_limited_until=float(payload.get("code_rate_limited_until") or 0.0),
        )


@dataclass
class HydrateResult:
    restored: List[MailOrder] = field(default_factory=list)
    stale: List[MailOrder] = field(default_factory=list)


_pool_lock = threading.Lock()
_inflight: dict[str, MailOrder] = {}
_available: dict[str, MailOrder] = {}
_tls = threading.local()
_store: Any = None
_hydrated = False


def reset_pool() -> None:
    global _store, _hydrated
    with _pool_lock:
        _inflight.clear()
        _available.clear()
        _store = None
        _hydrated = False
    _tls.email = ""


def bind_store(store: Any) -> None:
    """挂上仓储。需要提供 save_mailnest_order / delete_mailnest_order / list_mailnest_orders。"""
    global _store, _hydrated
    with _pool_lock:
        if store is _store:
            return
        _store = store
        _hydrated = False


def _persist(order: MailOrder, state: str) -> None:
    store = _store
    if store is None:
        return
    try:
        store.save_mailnest_order(order.to_payload(state))
    except Exception:
        # 落盘失败不能影响注册；最坏情况是重启后少复用一个邮箱。
        pass


def _forget(email: str) -> None:
    store = _store
    if store is None or not email:
        return
    try:
        store.delete_mailnest_order(email)
    except Exception:
        pass


def _discard_locked(key: str) -> None:
    _inflight.pop(key, None)
    _available.pop(key, None)
    _forget(key)


def hydrate() -> HydrateResult:
    """从仓储恢复上次进程留下的订单，每个进程只做一次。

    已扣费且还有余量的邮箱直接放回复用队列；没扣费的先记为在用并返回，
    由调用方去调释放接口退回冻结金额。
    """
    global _hydrated
    result = HydrateResult()
    with _pool_lock:
        store = _store
        if store is None or _hydrated:
            return result
        _hydrated = True
        try:
            rows = list(store.list_mailnest_orders() or [])
        except Exception:
            return result
        now = time.time()
        for row in rows:
            try:
                order = MailOrder.from_payload(dict(row))
            except Exception:
                continue
            key = order.email
            if not key or key in _inflight or key in _available:
                continue
            if not order.code_received:
                _inflight[key] = order
                result.stale.append(order)
                continue
            if not order.reusable(now):
                _forget(key)
                continue
            # 重启后剩下来的已扣费邮箱就是那一轮重试，不能再往后发一次。
            order.retry_used = True
            _available[key] = order
            _persist(order, STATE_AVAILABLE)
            result.restored.append(order)
    return result


def active_email() -> str:
    return str(getattr(_tls, "email", "") or "")


def _remember_active(email: str) -> None:
    _tls.email = str(email or "")


def parse_time(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=API_TIMEZONE)
    return parsed.timestamp()


def _order_from_payload(payload: dict) -> MailOrder:
    expired_text = str(payload.get("expired_at") or "")
    return MailOrder(
        email=str(payload.get("email") or "").strip(),
        order_id=str(payload.get("id") or ""),
        expired_at=parse_time(expired_text),
        expired_at_text=expired_text,
    )


def track_order(order: MailOrder) -> MailOrder:
    with _pool_lock:
        _inflight[order.email] = order
        _available.pop(order.email, None)
        _persist(order, STATE_INFLIGHT)
    _remember_active(order.email)
    return order


def get_order(email: str) -> Optional[MailOrder]:
    key = str(email or "").strip()
    with _pool_lock:
        return _inflight.get(key) or _available.get(key)


def is_inflight(email: str) -> bool:
    key = str(email or "").strip()
    with _pool_lock:
        return key in _inflight


def code_was_received(email: str) -> bool:
    order = get_order(email)
    return bool(order and order.code_received)


def received_code_count(email: str) -> int:
    order = get_order(email)
    if order is None:
        return 0
    return len(order.used_codes)


def remember_received_code(email: str, code: str, received_at: float = 0.0) -> None:
    key = str(email or "").strip()
    with _pool_lock:
        order = _inflight.get(key) or _available.get(key)
        if order is None:
            return
        order.code_received = True
        order.last_code_at = max(order.last_code_at, time.time())
        cleaned = str(code or "").strip()
        if cleaned:
            order.used_codes.add(cleaned)
        if received_at:
            order.last_received_at = max(order.last_received_at, received_at)
        _persist(order, STATE_INFLIGHT if key in _inflight else STATE_AVAILABLE)


def mark_charged(email: str) -> None:
    """MailNest 说这个地址已扣费，说明有验证码到过。按刚收到验证码处理。"""
    key = str(email or "").strip()
    with _pool_lock:
        order = _inflight.get(key) or _available.get(key)
        if order is None:
            return
        order.code_received = True
        order.last_code_at = max(order.last_code_at, time.time())
        _persist(order, STATE_INFLIGHT if key in _inflight else STATE_AVAILABLE)


def claim_reusable(blocked: Optional[BlockedEmail] = None, now: Optional[float] = None) -> Optional[MailOrder]:
    """取出一个已扣费、还没用完额外一轮、且离过期超过 3 分钟的邮箱。"""
    current = time.time() if now is None else now
    with _pool_lock:
        for email in list(_available):
            order = _available.get(email)
            if order is None:
                continue
            if blocked and blocked(email):
                _discard_locked(email)
                continue
            if not order.reusable(current):
                _discard_locked(email)
                continue
            _available.pop(email, None)
            _inflight[email] = order
            _persist(order, STATE_INFLIGHT)
            _remember_active(email)
            return order
    return None


def drop_order(email: str) -> None:
    key = str(email or "").strip()
    if not key:
        return
    with _pool_lock:
        _discard_locked(key)
    if active_email() == key:
        _remember_active("")


def recycle_order(email: str, *, reason: str = "", now: Optional[float] = None) -> bool:
    """扣费后的邮箱放回队列，只给一轮，而且马上可以取走。

    已经发过这一轮、当前不够 3 分钟余量，或者尚未扣费，则不复用。
    """
    key = str(email or "").strip()
    current = time.time() if now is None else now
    with _pool_lock:
        order = _inflight.get(key) or _available.get(key)
        if order is None or order.retry_used or not order.reusable(current):
            _discard_locked(key)
            if active_email() == key:
                _remember_active("")
            return False
        order.retry_used = True
        if reason == "sso_timeout":
            order.sso_timeout_reused = True
        _inflight.pop(key, None)
        _available[key] = order
        _persist(order, STATE_AVAILABLE)
    if active_email() == key:
        _remember_active("")
    return True


def prepare_sso_timeout_retry(email: str, now: Optional[float] = None) -> bool:
    """同一个已扣费邮箱只因 SSO 超时再试一次。这一轮用掉后不再另给。"""
    key = str(email or "").strip()
    current = time.time() if now is None else now
    with _pool_lock:
        order = _inflight.get(key)
        if (
            order is None
            or not order.code_received
            or order.retry_used
            or order.sso_timeout_reused
            or not order.reusable(current)
        ):
            return False
        order.retry_used = True
        order.sso_timeout_reused = True
        _inflight.pop(key, None)
        _available[key] = order
        _persist(order, STATE_AVAILABLE)
    if active_email() == key:
        _remember_active("")
    return True


def buy_order(http_post: HttpPost, api_key: str, project_code: str = "") -> MailOrder:
    code = (project_code or "").strip() or DEFAULT_PROJECT_CODE
    key = (api_key or "").strip()
    if not key:
        raise Exception(f"请在配置文件中配置 mailnest_api_key | 注册网址：{API_BASE}")
    resp = http_post(
        f"{API_BASE}/api/v1/email/temporary/buy",
        headers={"Authorization": f"Bearer {key}"},
        json={"project_code": code, "count": 1},
        timeout=30,
    )
    try:
        resp_json = resp.json()
    except Exception as exc:
        raise Exception(f"MailNest 买号响应无效: {exc}; body={resp.text[:300]}") from exc
    if str(resp_json.get("code")) != "00000":
        raise Exception(f"MailNest 买号失败: {resp.text[:500]}")
    data = resp_json.get("data") or []
    if not data or not data[0].get("email"):
        raise Exception(f"MailNest 买号无邮箱: {resp.text[:500]}")
    return _order_from_payload(data[0])


def buy_email(http_post: HttpPost, api_key: str, project_code: str = "") -> str:
    return buy_order(http_post, api_key, project_code).email


def release_email(http_post: HttpPost, api_key: str, email: str) -> str:
    """释放未扣费邮箱。返回 released、charged 或 failed。"""
    key = (api_key or "").strip()
    address = str(email or "").strip()
    if not key:
        raise Exception(f"请在配置文件中配置 mailnest_api_key | 注册网址：{API_BASE}")
    if not address:
        return "failed"
    resp = http_post(
        f"{API_BASE}/api/v1/email/release",
        headers={"Authorization": f"Bearer {key}"},
        json={"email": address},
        timeout=30,
    )
    try:
        resp_json = resp.json()
    except Exception as exc:
        raise Exception(f"MailNest 释放响应无效: {exc}") from exc
    if not isinstance(resp_json, dict):
        return "failed"
    code = str(resp_json.get("code") or "")
    if code == "00000":
        return "released"
    if code == ALREADY_CHARGED:
        return "charged"
    return "failed"


def receive_email(http_post: HttpPost, api_key: str, email: str) -> List[dict]:
    key = (api_key or "").strip()
    if not key:
        raise Exception(f"请在配置文件中配置 mailnest_api_key | 注册网址：{API_BASE}")
    resp = http_post(
        f"{API_BASE}/api/v1/email/receive",
        headers={"Authorization": f"Bearer {key}"},
        json={"email": email},
        timeout=30,
    )
    try:
        resp_json = resp.json()
    except Exception as exc:
        raise Exception(f"MailNest 收信响应无效: {exc}; body={resp.text[:300]}") from exc
    response_code = str(resp_json.get("code") or "")
    if response_code == ALREADY_CHARGED:
        message = str(resp_json.get("msg") or "")
        detail = f"{ALREADY_CHARGED}: {message}" if message else ALREADY_CHARGED
        raise ReceiveUnavailableError(email, detail)
    if response_code != "00000":
        raise Exception(f"MailNest 收信失败: {resp.text[:500]}")
    return resp_json.get("data") or []


def _mail_is_new(order: Optional[MailOrder], code: str, received_at: float) -> bool:
    if order is None:
        return True
    if order.last_received_at and received_at and received_at <= order.last_received_at:
        return False
    if not received_at and code and code in order.used_codes:
        return False
    return True


def wait_for_code(
    http_post: HttpPost,
    api_key: str,
    email: str,
    *,
    timeout: int = 60,
    poll_interval: int = 3,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
) -> str:
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            mails = receive_email(http_post, api_key, email)
        except ReceiveUnavailableError:
            raise
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] MailNest 拉取邮件失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        order = get_order(email)
        for mail in mails or []:
            if not isinstance(mail, dict):
                continue
            mail_id = str(mail.get("id") or mail.get("message_id") or "")
            preview = str(mail.get("body_preview") or mail.get("text") or mail.get("body") or "")
            subject = str(mail.get("subject") or "")
            fingerprint = mail_id or f"{subject}|{preview[:80]}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            if log_callback:
                log_callback(f"[Debug] MailNest 收到邮件: {subject or fingerprint}")
            code = str(mail.get("code_match") or "").strip() or extract_verification_code(
                f"{subject}\n{preview}",
                subject,
            )
            received_at = parse_time(mail.get("received_at"))
            if not code or not _mail_is_new(order, code, received_at):
                continue
            remember_received_code(email, code, received_at)
            if log_callback:
                log_callback(f"[*] MailNest 从邮件中提取到验证码: {code}")
            return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"MailNest 在 {timeout}s 内未收到验证码邮件")
