#!/usr/bin/env python3
"""
TruePeopleSearch 生产级告警与监控通知模块 (tps_alert.py)
支持钉钉 (DingTalk)、企业微信 (WeCom)、飞书 (Feishu)、Telegram 及通用 HTTP Webhook。
具备防抖节流 (Rate Limiting)，避免网络或代理故障时告警风暴。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Optional, Dict, Any

# 节流间隔：同一级别的告警在 cooldown 秒内最多发送一次
ALERT_COOLDOWN_SEC = 300
_LAST_ALERT_TIMES: Dict[str, float] = {}


def _customer_release_mode() -> bool:
    """Customer dashboard launches must not initiate external notifications."""
    return os.environ.get("TPS_RELEASE_MODE", "").strip().casefold() == "customer"


def send_alert(
    title: str,
    message: str,
    level: str = "WARNING",
    webhook_url: Optional[str] = None,
    force: bool = False,
    outbound_enabled: Optional[bool] = None,
) -> bool:
    """
    发送告警通知。
    webhook_url 默认读取环境变量 TPS_ALERT_WEBHOOK。
    level 可选: INFO, WARNING, ERROR, CRITICAL
    """
    # This check deliberately happens before URL selection, payload creation,
    # throttling, or network setup.  An explicit URL and force=True cannot
    # bypass the customer-release boundary.
    if outbound_enabled is False or _customer_release_mode():
        return False

    url = (webhook_url or os.environ.get("TPS_ALERT_WEBHOOK", "")).strip()
    if not url:
        # 未配置 Webhook 时静默返回，仅打印日志
        print(f"[ALERT:{level}] {title} - {message}", file=sys.stderr)
        return False

    now = time.time()
    dedup_key = f"{level}:{title}"
    if not force:
        last = _LAST_ALERT_TIMES.get(dedup_key, 0.0)
        if now - last < ALERT_COOLDOWN_SEC:
            return False

    # 构造兼容常用办公平台的 payload
    payload: Dict[str, Any] = {}
    lower_url = url.lower()

    if "dingtalk.com" in lower_url:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"[{level}] {title}",
                "text": f"### [{level}] {title}\n\n{message}\n\n*时间: {time.strftime('%Y-%m-%d %H:%M:%S')}*",
            },
        }
    elif "feishu.cn" in lower_url or "larksuite.com" in lower_url:
        payload = {
            "msg_type": "text",
            "content": {
                "text": f"[{level}] {title}\n{message}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
            },
        }
    elif "weixin.qq.com" in lower_url:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"### <font color=\"warning\">[{level}] {title}</font>\n>{message}\n>时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
            },
        }
    else:
        # 通用 Webhook JSON
        payload = {
            "level": level,
            "title": title,
            "message": message,
            "timestamp": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "TPS-Monitor-Alert/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            success = 200 <= resp.status < 300
            if success:
                _LAST_ALERT_TIMES[dedup_key] = now
            return success
    except Exception as exc:
        print(f"[ALERT_FAIL] 发送告警失败: {exc}", file=sys.stderr)
        return False
