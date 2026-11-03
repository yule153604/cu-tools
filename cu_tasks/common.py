"""共享配置与无会话工具函数，不依赖具体活动。"""

import base64
import os
import re
import threading
import traceback
from collections.abc import Mapping

import httpx
from Crypto.Cipher import AES, DES
from Crypto.Util.Padding import pad, unpad


def read_int_env(name, default=None, minimum=None):
    """模块启动时读取整数环境变量。未设置才用 default；空/非整数/低于最小值 raise。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"环境变量 {name}={raw!r} 不是合法整数") from e
    if minimum is not None and value < minimum:
        raise ValueError(f"环境变量 {name}={raw!r} 低于最小值 {minimum}")
    return value


# 常量
APP_VERSION = "iphone_c@11.0503"
UA = f"Mozilla/5.0 (iPhone; CPU iPhone OS 16_1_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 unicom{{version:{APP_VERSION}}}"
APP_ID = "86b8be06f56ba55e9fa7dff134c6b16c62ca7f319da4a958dd0afa0bf9f36f1daa9922869a8d2313b6f2f9f3b57f2901f0021c4575e4b6949ae18b7f6761d465c12321788dcd980aa1a641789d1188bb"
SEC_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15"

# 通通农场常量
TTXC_BASE_URL = "https://epay.10010.com/cu-ca-game-front"
TTXC_APP_BASE_URL = "https://epay.10010.com/cu-ca-app-front"
TTXC_CHANNEL = "225"
TTXC_REFERER = "https://epay.10010.com/cu-ca-game-web/index.html?channel=qdqp"
TTXC_UA = "Mozilla/5.0 (Linux; Android 10; MI 8 Build/QKQ1.190828.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/143.0.7499.146 Mobile Safari/537.36; unicom{version:android@11.0802,desmobile:0};devicetype{deviceBrand:Xiaomi,deviceModel:MI 8}"
TTXC_GARBAGE_WAIT_SECONDS = read_int_env(
    "UNICOM_TTXC_GARBAGE_WAIT", default=28, minimum=0
)
TTXC_GROW_MAX_CHARGE_PER_LAND = read_int_env(
    "UNICOM_TTXC_GROW_MAX_CHARGE_PER_LAND", default=20, minimum=0
)
TTXC_HARVEST_WAIT_SECONDS = read_int_env(
    "UNICOM_TTXC_HARVEST_WAIT", default=3, minimum=0
)
MARKET_WAIT_MAX_SECONDS = read_int_env("UNICOM_MARKET_WAIT_MAX", default=300, minimum=0)
MARKET_API_RETRIES = read_int_env("UNICOM_MARKET_API_RETRIES", default=3, minimum=1)
MARKET_API_RETRY_BASE_SECONDS = 0.15
TTXC_NEWBIE_STEPS = [
    "G01",
    "G02",
    "G03",
    "G03_2",
    "G04",
    "G05",
    "G09",
    "G10",
    "G11",
    "G12",
]

# 云盘活动签名密钥（上传大比拼等 panservice 活动共用）
CLOUD_PAN_SIGN_SECRET = "s8Hf3LqP9xN2vM5bR7tY1wZ4cA6eG0K"

BATTLE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko)  unicom{version:iphone_c@12.1300};ltst;OSVersion/27.0"
)
CLOUD_BATTLE_FILEINFO_IV = "wNSOYIB1k1DjY5lA"

# 云盘上传大比拼（2026-09-14 抓包为 38 期：页面 uploadBattle、上传域名 hyupload）
# activityId 每期更换（30 → 38），下期只需改环境变量 UNICOM_BATTLE_ACTIVITY_ID
CLOUD_BATTLE_ACTIVITY_ID = os.environ.get("UNICOM_BATTLE_ACTIVITY_ID", "Mzg=")
CLOUD_BATTLE_TOUCHPOINT = "300200030001"
CLOUD_BATTLE_PAGE = "uploadBattle"
# 上传域名：38 期实测 hyupload。可用 UNICOM_BATTLE_UPLOAD_URL 覆盖（逗号分隔多个按序尝试）
# 与校园季(CAMPUS_UPLOAD_URL)独立配置，不共用回退域名
CLOUD_BATTLE_UPLOAD_URLS = tuple(
    u.strip()
    for u in os.environ.get(
        "UNICOM_BATTLE_UPLOAD_URL",
        "https://hyupload.pan.wo.cn/openapi/client/upload2C",
    ).split(",")
    if u.strip()
)
CLOUD_BATTLE_FILE_NAME = os.environ.get("UNICOM_CLOUD_BATTLE_FILE", "文本.txt")
CLOUD_BATTLE_FILE_CONTENT = os.environ.get("UNICOM_CLOUD_BATTLE_CONTENT", "1")

# 沃视频(5G宽视界)内容转存：密钥逆向自 YunPanVideoV4 前端 chunk
WO_VIDEO_BASE = "https://mbh.chinaunicomvideo.cn"
WO_VIDEO_CHANNEL = "1001000218"
WO_TRANSFER_DES_KEY = "LsgFQAYhXkjpqB7K"
WO_TRANSFER_AES_KEY = "9b54bfce4b4b4719"
WO_TRANSFER_AES_IV = "wNSOYIB1k1DjY5lA"
# 校园季转存兜底内容（正常路径从校园 tab 动态发现，兜底 id 抓包验证可用）
CAMPUS_EDU_FALLBACK_ID = os.environ.get(
    "UNICOM_CAMPUS_EDU_ID", "0132429091704a6297fef31c7a6e2c5e"
)
CAMPUS_ENT_FALLBACK_ID = os.environ.get(
    "UNICOM_CAMPUS_ENT_ID", "746e403f3b6348328d3c954edf3fd06e"
)
CAMPUS_FALLBACK_NAME = {
    "edu": "考研数学概率论与数理统计第1集",
    "ent": "寒战1994粤语版",
}


def wo_des_b64(text, key=WO_TRANSFER_DES_KEY):
    """沃视频 DES-CBC（key 取前 8 字节，iv=0x01..0x08，Pkcs7）→ base64。"""
    cipher = DES.new(key.encode()[:8], DES.MODE_CBC, bytes(range(1, 9)))
    return base64.b64encode(cipher.encrypt(pad(text.encode(), 8))).decode()


def wo_aes_b64(text, key=WO_TRANSFER_AES_KEY):
    """沃视频 AES-CBC（固定 IV，Pkcs7）→ base64。"""
    cipher = AES.new(key.encode(), AES.MODE_CBC, WO_TRANSFER_AES_IV.encode())
    return base64.b64encode(cipher.encrypt(pad(text.encode(), 16))).decode()


def wo_aes_unb64(data, key=WO_TRANSFER_AES_KEY):
    """沃视频 AES-CBC 解密（JudgeFileTransferStatus 响应 DATA）。"""
    cipher = AES.new(key.encode(), AES.MODE_CBC, WO_TRANSFER_AES_IV.encode())
    return unpad(cipher.decrypt(base64.b64decode(data)), 16).decode()


SHANGDU_BASE = "https://app.shangdu.com"
SHANGDU_ENTRY = f"{SHANGDU_BASE}/monthlyBenefit/static/index.html"
SHANGDU_UA = "ChinaUnicom4.x/13.1 (com.chinaunicom.mobilebusiness; build:14; iOS 27.0.0) Alamofire/4.7.3 unicom{version:iphone_c@13.0100}"
SHANGDU_APP_ID = "edop_unicom_4b80047a"
SHANGDU_CHANNEL = "KSTD"
SHANGDU_SIGN_SECRET = "Hwsm6r3afijk8CP9d5F4nZu9DDyeMKMJxRpUqJqxCZ4sZuiyRU"
SHANGDU_SM2_PRIVATE_KEY = (
    "03659DDCBCA7513BDD930D4E19B8C216ECB714F1A70ACA2C155BCDD5886283F5"
)
SHANGDU_LOTTERY_PRIZES = [
    "5元话费券",
    "印鸽魔哒熊 电动牙刷",
    "DIY掌中宝照片书",
    "免费领麻布风手提包",
    "免费领定制抱枕套",
    "联通云盘 黄金会员-月卡",
    "爱奇艺黄金VIP会员-月卡",
    "腾讯视频VIP会员-月卡",
]
SHANGDU_LOTTERY_FESTIVAL_PRIZES = ["30元话费券", *SHANGDU_LOTTERY_PRIZES]
SHANGDU_LOTTERY_MAX = read_int_env("SHANGDU_LOTTERY_MAX", default=None, minimum=0)

# 云手机积分 / 夏日刮一刮 / 打卡挑战赛
UPHONE_H5API = "https://uphone.wostore.cn/h5api"
UPHONE_WO_ADV = "https://uphone.wo-adv.cn"
UPHONE_H5FORPHONE = "https://h5forphone.wostore.cn/h5forphone"
UPHONE_CHECKIN_PAGE = "https://h5forphone.wostore.cn/changXiangVip/CLD13.html"
UPHONE_CHANNEL = "ST-Wode"
UPHONE_CHANNEL_H5 = "ST-Jingang002"
UPHONE_EDOP_APP_ID = "edop_unicom_68e8fa69"

UPHONE_ACT_SIGN = "Points_Sign_2507"
UPHONE_ACT_OBTAIN = "Points_Obtain_2507"
UPHONE_ACT_EXCHANGE = (
    "Points_Exchange_2507"  # 2508 无效; 2507 列表无十连商品(2026031010) → 十连暂不可用
)
UPHONE_ACT_SUMMER = "HD2026062200218"
UPHONE_GOODS_LOTTERY_10 = "2026031010"
UPHONE_LOTTERY_COST = read_int_env("UNICOM_UPHONE_LOTTERY_COST", default=100, minimum=1)
UPHONE_OBTAIN_SKIP = frozenset({"012-4", "TASK2026010601"})
# 夏日刮一刮仅领此任务换抽奖次数，避免回退到 0514-001 等日限任务
UPHONE_SUMMER_CLAIM_TASK = "2508-01"
# 打卡挑战赛：需手填 QQ/微信 的奖品类型，跳过自动领取
UPHONE_CHECKIN_SKIP_ACTIVITY_TYPES = frozenset({2, "2"})
UPHONE_APP_UA = (
    "ChinaUnicom4.x/12.13 (com.chinaunicom.mobilebusiness; build:1; iOS 27.0.0) "
    "Alamofire/4.7.3 unicom{version:iphone_c@12.1300}"
)
UPHONE_H5_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko)  unicom{version:iphone_c@12.1300};ltst;OSVersion/27.0"
)


def require_int(value, field, minimum=None):
    """必填业务整数：仅接受非 bool 的 int，或可由 int() 完整解析的 str；可选 minimum 下限。"""
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
    r'(["\']?(token|ticket|cookie|verifycode|accesstoken|tokenid|authinfo|sessionid|'
    r'authorization|access[-_]?token|secret)["\']?\s*[:=]\s*)(["\']?)[^"\',}&\s]+\3',
    re.IGNORECASE,
)


def redact_text(text):
    """JSON/表单风格文本中的凭据字段打码（认证材料不入持久日志）。"""
    redacted = _REDACT_FIELD_RE.sub(r"\1\3***\3", str(text))
    return re.sub(r"(?<!\w)1[3-9]\d{9}(?!\w)", "***", redacted)


def response_summary(res):
    if isinstance(res, dict):
        meta = res.get("meta") or {}
        if isinstance(meta, dict) and meta.get("message"):
            return redact_text(meta.get("message"))
        if res.get("msg"):
            return redact_text(res.get("msg"))
    return redact_text(res)[:120]


def format_exception_detail(e, action=None):
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
    return redact_text(" | ".join(parts))


def api_response_err(r, action=None):
    parts = []
    if action:
        parts.append(action)
    if not r:
        parts.append("无响应")
        return " | ".join(parts)
    if r.get("code") == -1:
        parts.append("网络/请求异常")
        if r.get("error"):
            parts.append(str(r["error"]))
        return " | ".join(parts)
    http = r.get("code")
    if http not in (200, 201, 302):
        parts.append(f"HTTP={http}")
    if r.get("error"):
        parts.append("协议/JSON解析错误")
        parts.append(str(r["error"]))
        return " | ".join(parts)
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
            parts.append(f"body={redact_text(data)[:150]}")
    elif data is not None:
        text = str(data).strip()
        if text:
            parts.append(f"body={redact_text(text)[:150]}")
    elif http in (200, 201):
        parts.append("响应体为空")
    return " | ".join(parts)


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


NOTIFY_LINES = []


def notify_send(title, content):
    # notify.py 的一言接口无超时、推送线程 join 也无超时，必须隔离，否则会卡死青龙任务槽
    os.environ.setdefault("HITOKOTO", "false")

    def _send():
        try:
            from notify import send

            send(title, content)
        except Exception as e:
            print(f"推送失败: {e}")

    worker = threading.Thread(target=_send, daemon=True)
    worker.start()
    worker.join(timeout=30)
    if worker.is_alive():
        print("推送超时 30s，放弃等待")


def iter_set_cookies(headers: httpx.Headers | Mapping[str, object]):
    """逐条产出 Set-Cookie 值，避免按逗号拆分误伤 Expires 日期。"""
    if isinstance(headers, httpx.Headers):
        for item in (
            headers.get_list("set-cookie") or headers.get_list("Set-Cookie") or []
        ):
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
