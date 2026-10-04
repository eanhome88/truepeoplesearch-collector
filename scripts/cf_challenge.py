#!/usr/bin/env python3
"""
Cloudflare / 站内验证——挑战分型 + 最便宜优先路由。

为什么需要分型：过去所有“过不去”统一按 rate_limit 退避，但不同死因的最优解法
完全不同，用错手段等于烧钱：
  turnstile      CF Turnstile 小部件        -> 网关/求解器能解，换 IP 没用
  managed        CF Managed Challenge      -> 真浏览器多等几秒常自解，最便宜
  site_captcha   站内 InternalCaptcha       -> 跟 CF 无关，换出口/换 lane 才有用
  rate_limit     429 / TOO_MANY_REQUESTS    -> 全局退避，任何重试都浪费
  ip_block       403+AccessDenied/1015/1020 -> 该 IP 已死，立即换代理并踢掉暖机 Cookie
  origin_fail    源站 5xx                  -> 同出口稍后重试即可
  empty_block    空包/超短正文             -> 换组重试（代理层问题）
  timeout        传输超时                   -> 换组重试
  clean / unknown                         -> 非挑战 / 未知失败

路由表按“便宜优先”排：浏览器自解(0 成本) > 暖机 Cookie(0) > 网关(按次计费) >
求解器(自建成本) > 换代理(会话成本) > 全局退避(时间成本)。调用方按 caps
（网关开没开、有没有求解器）取第一条可用动作。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

# 分型结果常量
CLEAN = "clean"
TURNSTILE = "turnstile"
MANAGED = "managed"
SITE_CAPTCHA = "site_captcha"
RATE_LIMIT = "rate_limit"
IP_BLOCK = "ip_block"
ORIGIN_FAIL = "origin_fail"
EMPTY_BLOCK = "empty_block"
TIMEOUT = "timeout"
UNKNOWN_FAIL = "unknown_fail"

KINDS = (
    CLEAN, TURNSTILE, MANAGED, SITE_CAPTCHA, RATE_LIMIT,
    IP_BLOCK, ORIGIN_FAIL, EMPTY_BLOCK, TIMEOUT, UNKNOWN_FAIL,
)

CHALLENGE_KEY = "tps:challenge"

# 动作常量（调用方按名执行，顺序即优先级）
BROWSER_WAIT = "browser_wait"      # 真浏览器等待自解，0 成本
WARMED_RETRY = "warmed_retry"     # 换暖机 Cookie 同出口重试，0 成本
GATEWAY = "gateway"               # 穿云网关，按次计费
SOLVER = "solver"                 # 自研求解器，自建成本
ROTATE_PROXY = "rotate_proxy"     # 换出口，会话成本
LANE_SWITCH = "lane_switch"       # 切备用 lane
BACKOFF = "backoff"               # 全局退避，时间成本
RETRY_OTHER_GROUP = "retry_other_group"  # 换组重排
RETRY_SAME = "retry_same"         # 同出口稍后重试

_TURNSTILE_MARKERS = (
    "cf-turnstile", "turnstile", "challenges.cloudflare.com",
    "cf_chl_", "__cf_chl_",
)
_MANAGED_MARKERS = (
    "just a moment", "cf-challenge", "checking your browser",
    "cf-browser-verification", "challenge-platform",
    "请稍候",
)
# 注意："attention required" 故意不在上面——它是 CF 1020/1015 硬拒绝页的标题，
# 老 IUAM 页同时带 checking-your-browser 文案，会先命中 MANAGED（真能自解，正确）。
_SITE_MARKERS = ("internalcaptcha",)
_RATE_MARKERS = ("too_many_requests", "too many requests", "rate limited", "rate_limit")
_IP_BLOCK_MARKERS = (
    "error code: 1015", "error code: 1020", "error code: 1010",
    "access denied", "ip banned", "ip blocked",
)
_ORIGIN_STATUS = (500, 502, 503, 504)


def _blob(*parts: object) -> str:
    out = []
    for p in parts:
        if isinstance(p, BaseException):
            out.append(str(p))
        elif isinstance(p, (bytes, bytearray)):
            out.append(p.decode("utf-8", errors="replace"))
        elif isinstance(p, str):
            out.append(p)
        elif p is not None:
            out.append(str(p))
    return "\n".join(out).lower()


def _status_of(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def classify(
    url: str = "",
    html: str = "",
    status: Optional[int] = None,
    headers: Any = None,
    exc: Optional[BaseException] = None,
) -> str:
    """纯函数：按 (状态码 > URL > 正文 > 异常) 分型，返回 KINDS 之一。"""
    blob = _blob(url, html, exc)
    code = status if status is not None else (_status_of(exc) if exc is not None else None)

    # 1. 传输层超时：看异常文案，与页面无关
    if exc is not None and any(
        s in _blob(exc) for s in ("timeout", "timed out", "err_timed_out", "timeouterror")
    ):
        return TIMEOUT
    # 空包：200 却几乎没内容（沿用 is_challenge_html 的 40 字符阈值），
    # 说明是代理层吐的空包。但短正文里若带挑战标记，仍按标记分型。
    if code == 200 and isinstance(html, str) and len(html.strip()) < 40:
        marked = (
            any(s in blob for s in _SITE_MARKERS)
            or any(s in blob for s in _TURNSTILE_MARKERS)
            or any(s in blob for s in _MANAGED_MARKERS)
            or any(s in blob for s in _RATE_MARKERS)
        )
        if not marked:
            return EMPTY_BLOCK

    # 2. 429 优先（网关/源站都可能吐 429）
    if code == 429 or any(s in blob for s in _RATE_MARKERS):
        return RATE_LIMIT

    # 3. 站内验证码：跟 CF 无关，必须先判（它常伴随 302/200）
    if any(s in blob for s in _SITE_MARKERS):
        return SITE_CAPTCHA

    # 4. Turnstile
    if any(s in blob for s in _TURNSTILE_MARKERS):
        return TURNSTILE

    # 5. CF Managed Challenge
    if any(s in blob for s in _MANAGED_MARKERS):
        return MANAGED

    # 6. IP 封禁：403 + 封禁标记。“Attention Required”是 CF 1020/1015
    # 拒绝页的标题，见到即判死刑；无标记的裸 403 归 unknown（可能是源站鉴权）。
    if code == 403 and (
        any(s in blob for s in _IP_BLOCK_MARKERS) or "attention required" in blob
    ):
        return IP_BLOCK

    # 7. 源站 5xx（无挑战标记才算源站问题）
    if code in _ORIGIN_STATUS:
        return ORIGIN_FAIL

    # 8. 200 且无任何标记：干净页
    if code == 200:
        return CLEAN
    if code in (404, 410):
        return CLEAN  # 号码不存在/人物删除：不是挑战，是正常空结果
    # 代理层空包类异常（无状态码无正文）：归 empty_block，路由换组重试
    if any(s in blob for s in ("err_empty_response", "empty response", "connection reset",
                               "connection closed", "broken pipe", "err_connection",
                               "err_socket", "err_proxy")):
        return EMPTY_BLOCK
    return UNKNOWN_FAIL


def classify_exception(exc: BaseException, url: str = "", page: Any = None) -> str:
    """异常路径分型：从异常 + 页面对象里抽 status/html 复用 classify。"""
    status = _status_of(exc)
    html = ""
    final_url = url
    if page is not None:
        for attr in ("html_content", "html", "body", "content"):
            try:
                val = getattr(page, attr, None)
            except Exception:
                continue
            if isinstance(val, (bytes, bytearray)):
                html = val.decode("utf-8", errors="replace")
                break
            if isinstance(val, str) and val.strip():
                html = val
                break
        for obj in (page, getattr(page, "response", None)):
            try:
                loc = getattr(obj, "url", None)
            except Exception:
                continue
            if loc:
                final_url = f"{url}\n{loc}"
                break
    return classify(url=final_url, html=html, status=status, exc=exc)


def capabilities() -> Dict[str, bool]:
    """当前进程可用的解法。网关/求解器开没开由环境变量决定。"""
    gateway = (os.environ.get("USE_CLOUDBYPASS", "1") == "1") and bool(
        (os.environ.get("CLOUDBYPASS_APIKEY") or "").strip()
    )
    solver = (os.environ.get("TPS_OWN_CF", "0") == "1") and bool(
        (os.environ.get("TPS_CF_SOLVER") or "").strip()
    )
    return {"browser": True, "warmed": True, "gateway": gateway, "solver": solver}


# 路由表：kind -> 按成本从低到高的动作序列。调用方取第一条 caps 允许的。
_ROUTES: Dict[str, List[str]] = {
    # Turnstile 必须真解：换 IP 没用，别浪费会话
    TURNSTILE: [WARMED_RETRY, GATEWAY, SOLVER, BROWSER_WAIT, BACKOFF],
    # Managed 真浏览器常自解：先等，再网关
    MANAGED: [BROWSER_WAIT, WARMED_RETRY, GATEWAY, SOLVER, ROTATE_PROXY],
    # 站内验证跟 CF 无关：网关/求解器都解不了，直接换出口
    SITE_CAPTCHA: [ROTATE_PROXY, LANE_SWITCH, BACKOFF],
    # 429：任何重试都浪费，先退避；退避后换组
    RATE_LIMIT: [BACKOFF, RETRY_OTHER_GROUP],
    # IP 已死：立刻换，别拿这 IP 再撞；同时要踢暖机 Cookie（调用方负责）
    IP_BLOCK: [ROTATE_PROXY, RETRY_OTHER_GROUP],
    # 源站抖动：同出口稍后重试最便宜
    ORIGIN_FAIL: [RETRY_SAME, BACKOFF],
    # 空包/超时：代理层问题，换组重排
    EMPTY_BLOCK: [RETRY_OTHER_GROUP, ROTATE_PROXY],
    TIMEOUT: [RETRY_OTHER_GROUP, ROTATE_PROXY],
    CLEAN: [RETRY_SAME],
    UNKNOWN_FAIL: [RETRY_OTHER_GROUP, BACKOFF],
}

_NEEDS_CAP = {GATEWAY: "gateway", SOLVER: "solver"}


def route_for(kind: str, caps: Optional[Dict[str, bool]] = None) -> List[str]:
    """返回该分型在当前 caps 下的可用动作序列（已过滤，顺序即优先级）。"""
    caps = caps if caps is not None else capabilities()
    plan = []
    for action in _ROUTES.get(kind, _ROUTES[UNKNOWN_FAIL]):
        need = _NEEDS_CAP.get(action)
        if need is not None and not caps.get(need, False):
            continue
        plan.append(action)
    return plan or [BACKOFF]


def first_action(kind: str, caps: Optional[Dict[str, bool]] = None) -> str:
    plan = route_for(kind, caps=caps)
    return plan[0] if plan else BACKOFF


def note_challenge(redis_client: Any, kind: str) -> None:
    """分型计数：tps:challenge hash 按 kind 累加，失败静默（观测不能炸主流程）。"""
    if kind not in KINDS:
        kind = UNKNOWN_FAIL
    try:
        redis_client.hincrby(CHALLENGE_KEY, kind, 1)
    except Exception:
        pass


def challenge_snapshot(redis_client: Any) -> Dict[str, int]:
    """读回各分型累计，供面板/排障用。"""
    try:
        raw = redis_client.hgetall(CHALLENGE_KEY) or {}
    except Exception:
        return {}
    out: Dict[str, int] = {}
    for k, v in raw.items():
        name = k.decode("utf-8", errors="replace") if isinstance(k, (bytes, bytearray)) else str(k)
        try:
            out[name] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def captcha_corpus_dir() -> str:
    """样本目录：TPS_CAPTCHA_CORPUS=0/off/none 关闭；否则 data/captcha_corpus（最多保留 50 个）。"""
    import os as _os
    raw = (_os.environ.get("TPS_CAPTCHA_CORPUS") or "").strip().lower()
    if raw in ("0", "false", "no", "off", "none", "disabled"):
        return ""
    return (_os.environ.get("TPS_CAPTCHA_CORPUS_DIR") or "data/captcha_corpus").strip() or "data/captcha_corpus"


def save_captcha_sample(html: str, url: str = "", job_id: str = "") -> str:
    """站内验证页存档：给免费 OCR（ddddocr 这类本地识别）攒样本。
    返回写盘路径；目录关掉/正文太短/异常时返回空串，绝不抛。"""
    import os as _os
    import re as _re
    import time as _time
    base = captcha_corpus_dir()
    if not base or not html or len(html.strip()) < 200:
        return ""
    try:
        _os.makedirs(base, exist_ok=True)
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        safe_jid = _re.sub(r"[^A-Za-z0-9_-]+", "_", str(job_id or "noj")[:24])
        path = _os.path.join(base, f"site_captcha-{stamp}-{safe_jid}.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"<!-- url: {url} -->\n")
            fh.write(html)
        files = sorted(
            (_os.path.join(base, n) for n in _os.listdir(base) if n.endswith(".html")),
            key=lambda p: _os.path.getmtime(p),
        )
        for stale in files[:-50]:
            try:
                _os.remove(stale)
            except OSError:
                pass
        return path
    except Exception:
        return ""
