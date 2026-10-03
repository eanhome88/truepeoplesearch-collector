#!/usr/bin/env python3
"""两份 Cookie/桥接 PoC 共用的粘性代理、人物页判定、出口 IP 探测。"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, urlparse, urlunparse

CAPTCHA_MARKERS = (
    "internalcaptcha",
    "just a moment",
    "cf-turnstile",
    "cf-challenge",
    "attention required",
    "checking your browser",
)

PERSON_MARKERS = (
    "phone numbers",
    "current address",
    "all relatives",
    "possible relatives",
)

IP_PROBE_URLS = (
    "https://api.ipify.org?format=json",
    "https://api64.ipify.org?format=json",
    "https://icanhazip.com/",
    "https://ifconfig.me/ip",
    "http://httpbin.org/ip",
)


def sticky_proxy_url(proxy_url: str, session_id: str, minutes: int = 30) -> str:
    """按代理类型写入粘性标记；同代理 URL 不等于同出口。"""
    raw = (proxy_url or "").strip()
    # 过短 sid 容易撞上池内残留坏会话；至少 6 位
    sid = re.sub(r"[^A-Za-z0-9]", "", session_id or "")[:12] or "poc"
    if len(sid) < 6:
        sid = (sid + "sess01")[:8]
    if not raw:
        return raw
    parts = urlparse(raw if "://" in raw else "http://" + raw)
    user = parts.username or ""
    if not user:
        return raw if "://" in raw else "http://" + raw
    host = (parts.hostname or "").lower()
    hold = max(1, min(int(minutes), 120))
    user_clean = re.sub(r"(?i)_s[a-z0-9]+-\d+[smhd]$", "", user)
    user_clean = re.sub(r"(?i)-sid-[A-Za-z0-9]+-t-\d+", "", user_clean)
    user_clean = re.sub(r"(?i)-session[_-][A-Za-z0-9]+", "", user_clean)

    if "cloudbypass" in host or "gw-res" in host or "-res_" in user.lower() or "-res-" in user.lower():
        username = f"{user_clean}_s{sid[:8]}-{hold}m"
        note = "cloudbypass/_s sticky"
    elif "-region-" in user.lower():
        username = f"{user_clean}-sid-{sid[:8]}-t-{hold}"
        note = "region -sid sticky"
    else:
        username = f"{user_clean}-session_{sid[:8]}"
        note = "generic -session_ sticky"

    password = parts.password or ""
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    host_str = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    sticky = urlunparse((parts.scheme or "http", f"{auth}@{host_str}{port}", parts.path or "", "", "", ""))
    print(f"[Sticky] 已写入粘性标记 ({note})")
    return sticky


def is_valid_person_response(status_code: int, text: str, url: str = "") -> tuple[bool, str]:
    """只有 200 + 非验证页 + ≥10KB + 含人物字段才算成功。"""
    blob = f"{url or ''}\n{text or ''}".lower()
    if status_code != 200:
        return False, f"HTTP状态码异常 ({status_code})"
    for marker in CAPTCHA_MARKERS:
        if marker in blob:
            return False, f"命中验证/拦截标记 '{marker}'"
    if len(text or "") < 10000:
        return False, f"页面过短 ({len(text or '')} 字节)，不是完整人物页"
    if not any(kw in blob for kw in PERSON_MARKERS):
        return False, "缺少人物字段 (Phone Numbers / Current Address 等)"
    return True, "成功捕获人物档案"


def _parse_ip_payload(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if raw.startswith("{"):
        try:
            import json

            data = json.loads(raw)
            for key in ("origin", "ip", "query"):
                val = data.get(key)
                if val:
                    # httpbin 可能返回 "a, b"
                    return str(val).split(",")[0].strip()
        except Exception:
            return ""
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", raw):
        return raw
    return ""


def probe_exit_ip(session, label: str = "IP", timeout: int = 12, rounds: int = 2) -> str:
    """用已配置代理的 session 探测出口 IP。多端点重试，失败返回空串。"""
    last_err = ""
    for _ in range(max(1, int(rounds))):
        for url in IP_PROBE_URLS:
            try:
                resp = session.get(url, timeout=timeout, headers={"accept": "*/*"})
                ip = _parse_ip_payload(getattr(resp, "text", "") or "")
                if ip:
                    print(f"[{label}] 出口 IP: {ip}  (via {urlparse(url).hostname})")
                    return ip
                last_err = f"HTTP {getattr(resp, 'status_code', '?')} 无法解析: {(getattr(resp, 'text', '') or '')[:60]}"
            except Exception as exc:
                last_err = str(exc)
    print(f"[{label}] 探测失败: {last_err}")
    return ""


def assert_same_exit_ip(expected: str, actual: str, where: str) -> str:
    """比较两次出口。返回 ok / drift / unknown。"""
    if not expected or not actual:
        print(
            f"[IP 一致性] {where}: 缺探测结果（expected={expected or '-'} actual={actual or '-'}）"
            " → 粘性未确认"
        )
        return "unknown"
    if expected == actual:
        print(f"[IP 一致性] {where}: 相同 ({actual})")
        return "ok"
    print(f"[IP 一致性] {where}: ❌ 漂移 {expected} -> {actual}。粘性失败，后续 Cookie 结论无效。")
    return "drift"


def sticky_label(status: str) -> str:
    return {
        "ok": "通过检查",
        "drift": "失败/漂移（Cookie 结论不可用）",
        "unknown": "未确认（探测失败，结论不可靠）",
    }.get(status, status)


def make_curl_session(proxy_url: Optional[str], impersonate: str = "chrome124"):
    from curl_cffi import requests as c_requests

    session = c_requests.Session(impersonate=impersonate)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session
