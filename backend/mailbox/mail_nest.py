"""MailNest 邮箱渠道适配器。

临时邮箱购买时只冻结金额。第一次成功收件才扣费，之后在 expired_at 前
还可以继续收信。未收到验证码时应调用释放接口，把冻结金额退回。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from backend.mailbox.utilities import extract_verification_code

API_BASE = "https://mailnest.top"
DEFAULT_PROJECT_CODE = "x-ai001"
REUSE_MARGIN_SECONDS = 180
ALREADY_CHARGED = "D0004"

HttpPost = Callable[..., Any]
BlockedEmail = Callable[[str], bool]


@dataclass
class MailOrder:
    email: str
    order_id: str = ""
    expired_at: float = 0.0
    expired_at_text: str = ""
    code_received: bool = False
    sso_timeout_reused: bool = False
    used_codes: set[str] = field(default_factory=set)
    last_received_at: float = 0.0

    def remaining_seconds(self, now: Optional[float] = None) -> int:
        if not self.expired_at:
            return 0
        return int(self.expired_at - (time.time() if now is None else now))

    def reusable(self, now: Optional[float] = None, margin: int = REUSE_MARGIN_SECONDS) -> bool:
        if not self.code_received or not self.expired_at:
            return False
        current = time.time() if now is None else now
        return self.expired_at - current > margin


_pool_lock = threading.Lock()
_inflight: dict[str, MailOrder] = {}
_available: dict[str, MailOrder] = {}
_tls = threading.local()


def reset_pool() -> None:
    with _pool_lock:
        _inflight.clear()
        _available.clear()
    _tls.email = ""


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
        parsed = parsed.replace(tzinfo=timezone.utc)
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
    _remember_active(order.email)
    return order


def get_order(email: str) -> Optional[MailOrder]:
    key = str(email or "").strip()
    with _pool_lock:
        return _inflight.get(key) or _available.get(key)


def code_was_received(email: str) -> bool:
    order = get_order(email)
    return bool(order and order.code_received)


def remember_received_code(email: str, code: str, received_at: float = 0.0) -> None:
    order = get_order(email)
    if order is None:
        return
    order.code_received = True
    cleaned = str(code or "").strip()
    if cleaned:
        order.used_codes.add(cleaned)
    if received_at:
        order.last_received_at = max(order.last_received_at, received_at)


def claim_reusable(blocked: Optional[BlockedEmail] = None, now: Optional[float] = None) -> Optional[MailOrder]:
    """取出一个已扣费且离过期还超过 3 分钟的邮箱。"""
    current = time.time() if now is None else now
    with _pool_lock:
        for email in list(_available):
            order = _available.get(email)
            if order is None:
                continue
            if blocked and blocked(email):
                _available.pop(email, None)
                continue
            if not order.reusable(current):
                _available.pop(email, None)
                continue
            _available.pop(email, None)
            _inflight[email] = order
            _remember_active(email)
            return order
    return None


def drop_order(email: str) -> None:
    key = str(email or "").strip()
    if not key:
        return
    with _pool_lock:
        _inflight.pop(key, None)
        _available.pop(key, None)
    if active_email() == key:
        _remember_active("")


def recycle_order(email: str, *, reason: str = "", now: Optional[float] = None) -> bool:
    """扣费后的邮箱放回队列。时间不够或尚未扣费时不复用。"""
    key = str(email or "").strip()
    current = time.time() if now is None else now
    with _pool_lock:
        order = _inflight.get(key) or _available.get(key)
        if order is None or not order.reusable(current):
            _inflight.pop(key, None)
            _available.pop(key, None)
            if active_email() == key:
                _remember_active("")
            return False
        if reason == "sso_timeout":
            order.sso_timeout_reused = True
        _inflight.pop(key, None)
        _available[key] = order
    if active_email() == key:
        _remember_active("")
    return True


def prepare_sso_timeout_retry(email: str, now: Optional[float] = None) -> bool:
    """同一个已扣费邮箱只因 SSO 超时再试一次。"""
    key = str(email or "").strip()
    current = time.time() if now is None else now
    with _pool_lock:
        order = _inflight.get(key)
        if (
            order is None
            or not order.code_received
            or order.sso_timeout_reused
            or not order.reusable(current)
        ):
            return False
        order.sso_timeout_reused = True
        _inflight.pop(key, None)
        _available[key] = order
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
    if str(resp_json.get("code")) != "00000":
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
