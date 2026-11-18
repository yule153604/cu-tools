"""与具体活动无关的工具函数：整数校验、响应摘要、日志脱敏、Cookie 解析。"""

import base64
import os
import re
import traceback
from collections.abc import Mapping

import httpx


def require_int(value, field, minimum=None):
    """必填业务整数：仅接受非 bool 的 int，或可由 int() 完整解析的 str。"""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}={value!r} 不是合法整数")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        try:
            n = int(value)
        except ValueError as e:
            raise ValueError(f"{field}={value!r} 不是合法整数") from e
    else:
        raise ValueError(f"{field}={value!r} 不是合法整数")
    if minimum is not None and n < minimum:
        raise ValueError(f"{field}={value!r} 低于最小值 {minimum}")
    return n


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_REDACT_FIELD_RE = re.compile(
    r'(["\']?[\w-]*?(?:token|ticket|cookie|verifycode|authinfo|sessionid|'
    r'authorization|secret|password|passwd)[\w-]*?["\']?\s*[:=]\s*(?:Bearer\s+)?)'
    r'(?:(["\'])(?!\2)[^"\']*(?:\2|$)|[^"\',}&\s]+)',
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_MOBILE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+?86[-\s]?)?(?<!\*{3})(1[3-9]\d{9})"
    r"(?:(?![A-Za-z0-9])|(?=\d{10,}(?![A-Za-z0-9])))"
)
_ORDER_MOBILE_PREFIX_RE = re.compile(
    r"^([A-Za-z]{0,8}?)(?:\+?86)?(?<!\*{3})(1[3-9]\d{9})(\d{4,})$"
)


def _redact_field_match(match: re.Match) -> str:
    quote = match.group(2) or ""
    return f"{match.group(1)}{quote}***{quote}"


def mask_mobile(mobile):
    if not mobile:
        return "未知号码"
    s = str(mobile)
    return f"{s[:3]}****{s[-4:]}" if len(s) >= 7 else "***"


def redact_text(text, mobile: str = ""):
    """JSON/表单风格文本中的凭据字段、Bearer 令牌与手机号打码。"""
    redacted = str(text)
    redacted = _BEARER_RE.sub(r"\1***", redacted)
    redacted = _REDACT_FIELD_RE.sub(_redact_field_match, redacted)
    if mobile and len(mobile) >= 7:
        redacted = redacted.replace(mobile, mask_mobile(mobile))
        if len(mobile) >= 11:
            b64 = base64.b64encode(mobile.encode()).decode()
            redacted = redacted.replace(b64, "***")
            b64_nopad = b64.rstrip("=")
            if b64_nopad != b64:
                redacted = redacted.replace(b64_nopad, "***")
    return _MOBILE_RE.sub("***", redacted)


def redact_order_id(order_id, mobile: str = "") -> str:
    """订单号脱敏：遮蔽已知手机号子串或内嵌手机号前缀，保留订单尾号定位。"""
    if order_id is None:
        return ""
    s = str(order_id).strip()
    if not s:
        return ""
    if mobile and len(mobile) >= 7 and mobile in s:
        s = s.replace(mobile, mask_mobile(mobile))
    m = _ORDER_MOBILE_PREFIX_RE.match(s)
    if m:
        s = f"{m.group(1)}{mask_mobile(m.group(2))}{m.group(3)}"
    return redact_text(s, mobile=mobile)


def response_summary(res, *, mobile: str = ""):
    if isinstance(res, dict):
        meta = res.get("meta") or {}
        if isinstance(meta, dict) and meta.get("message"):
            return redact_text(meta.get("message"), mobile=mobile)
        if res.get("msg"):
            return redact_text(res.get("msg"), mobile=mobile)
    return redact_text(res, mobile=mobile)[:120]


def format_exception_detail(e, action=None, *, mobile: str = ""):
    parts = []
    if action:
        parts.append(f"环节={action}")
    parts.append(f"{type(e).__name__}: {e}")
    if e.__traceback__:
        frames = traceback.extract_tb(e.__traceback__)
        if frames:
            f = frames[-1]
            parts.append(f"位置={os.path.basename(f.filename)}:{f.lineno}@{f.name}")
            if f.line:
                parts.append(f"代码={f.line.strip()}")
    if e.__cause__:
        parts.append(f"原因={type(e.__cause__).__name__}: {e.__cause__}")
    return redact_text(" | ".join(parts), mobile=mobile)


def api_response_err(r, action=None, *, mobile: str = ""):
    parts = []
    if action:
        parts.append(action)
    if not r:
        parts.append("无响应")
        return redact_text(" | ".join(parts), mobile=mobile)
    if r.get("code") == -1:
        parts.append("网络/请求异常")
        if r.get("error"):
            parts.append(str(r["error"]))
        return redact_text(" | ".join(parts), mobile=mobile)
    http = r.get("code")
    if http not in (200, 201, 302):
        parts.append(f"HTTP={http}")
    if r.get("error"):
        parts.append("协议/JSON解析错误")
        parts.append(str(r["error"]))
        return redact_text(" | ".join(parts), mobile=mobile)
    data = r.get("data")
    if isinstance(data, dict):
        for k in (
            "code",
            "status",
            "resultCode",
            "desc",
            "dsc",
            "msg",
            "message",
            "resultMsg",
            "resultDesc",
        ):
            v = data.get(k)
            if v not in (None, "", {}):
                parts.append(f"{k}={v}")
        if len(parts) == (1 if action else 0):
            parts.append(f"body={redact_text(data, mobile=mobile)[:150]}")
    elif data is not None:
        text = str(data).strip()
        if text:
            parts.append(f"body={redact_text(text, mobile=mobile)[:150]}")
    elif http in (200, 201):
        parts.append("响应体为空")
    return redact_text(" | ".join(parts), mobile=mobile)


def require_dict_data(r, action=None):
    data = r.get("data") if r else None
    if isinstance(data, dict):
        return data
    raise ValueError(api_response_err(r, action))


def is_retryable_api_response(r) -> bool:
    """网络超时、5xx、无 JSON 体等可重试；业务错误不重试。"""
    if not r:
        return True
    code = r.get("code")
    if code == -1:
        return True
    if isinstance(code, int) and code >= 500:
        return True
    if r.get("error") and not isinstance(r.get("data"), dict):
        return True
    return False


def iter_set_cookies(headers: httpx.Headers | Mapping[str, object]):
    """逐条产出 Set-Cookie 值，避免按逗号拆分误伤 Expires 日期。"""
    if isinstance(headers, httpx.Headers):
        for item in headers.get_list("set-cookie"):
            if item:
                yield item.strip()
        return
    sc = headers.get("set-cookie") or headers.get("Set-Cookie")
    if not sc:
        return
    if isinstance(sc, list):
        for item in sc:
            if item:
                yield item.strip()
    else:
        yield str(sc).strip()


def find_set_cookie(headers, name):
    """从响应头取指定 Cookie 的值；精确匹配键名，不存在返回空串。"""
    for item in iter_set_cookies(headers or {}):
        first_pair = item.split(";", 1)[0].strip()
        if "=" not in first_pair:
            continue
        key, value = first_pair.split("=", 1)
        if key.strip() == name:
            return value
    return ""
