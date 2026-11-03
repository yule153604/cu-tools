"""账号会话：在线登录、连接池与日志；活动任务通过 Account 获取隔离的客户端。"""

import random
import string
import uuid
from datetime import datetime

import httpx

from cu_tasks.common import (
    api_response_err,
    format_exception_detail,
    mask_mobile,
    redact_text,
)
from cu_tasks.net import new_client, request

APP_VERSION = "iphone_c@11.0503"
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_1_2 like Mac OS X) AppleWebKit/605.1.15 "
    f"(KHTML, like Gecko) Mobile/15E148 unicom{{version:{APP_VERSION}}}"
)
APP_ID = (
    "86b8be06f56ba55e9fa7dff134c6b16c62ca7f319da4a958dd0afa0bf9f36f1daa9922869a8d"
    "2313b6f2f9f3b57f2901f0021c4575e4b6949ae18b7f6761d465c12321788dcd980aa1a641789"
    "d1188bb"
)
LOGIN_URL = "https://m.client.10010.com/mobileService/onLine.htm"


def format_line(idx, msg, tag=None):
    scope = f"[账号{idx}] [{tag}]" if tag else f"[账号{idx}]"
    return f"[{datetime.now():%H:%M:%S}] {scope} {redact_text(msg)}"


class Account:
    """单个账号的登录态。登录 Cookie 存于 self.client，任务客户端由此复制。"""

    def __init__(self, token: str, idx: int = 1, *, transport=None):
        if not token.strip():
            raise ValueError("账号 token_online 不能为空")
        self.token, self.idx = token.strip(), idx
        self.mobile = self.province = self.province_code = self.ecs_token = ""
        self.uuid = str(uuid.uuid4())
        self.unicom_token_id = "".join(
            random.choices(string.ascii_letters + string.digits, k=32)
        )
        self.lines: list[str] = []
        self._transport = transport or httpx.AsyncHTTPTransport(http2=False)
        self.client = new_client(
            self._transport, headers={"User-Agent": UA, "Connection": "keep-alive"}
        )
        tid = self.unicom_token_id
        for name, value in (
            ("TOKENID_COOKIE", f"chinaunicom-{tid}"),
            ("UNICOM_TOKENID", tid),
            ("sdkuuid", tid),
        ):
            self.client.cookies.set(name, value, domain=".10010.com")

    # --- 日志 ---
    def emit(self, line: str, sink: list[str]):
        print(line, flush=True)
        sink.append(line)

    def log(self, msg):
        self.emit(format_line(self.idx, msg), self.lines)

    def log_exc(self, e, action=None):
        self.log(f"异常: {format_exception_detail(e, action)}")

    # --- HTTP ---
    def new_client(self) -> httpx.AsyncClient:
        """任务专用客户端：共享连接池，复制当前登录 Cookie 与默认请求头。"""
        return new_client(
            self._transport, headers=self.client.headers, cookies=self.client.cookies
        )

    async def req(self, method, url, **kw):
        return await request(self.client, method, url, **kw)

    async def close(self):
        await self.client.aclose()
        await self._transport.aclose()

    @property
    def closed(self) -> bool:
        return self.client.is_closed

    # --- 登录 ---
    async def login(self) -> bool:
        r = await self.req(
            "POST",
            LOGIN_URL,
            data={
                "token_online": self.token,
                "reqtime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "appId": APP_ID,
                "version": APP_VERSION,
                "step": "bindlist",
                "isFirstInstall": 0,
                "deviceModel": "iPhone14,6",
                "uniqueIdentifier": "ios"
                + "".join(random.choices("0123456789abcdef", k=32)),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        res = r["data"]
        if not isinstance(res, dict) or str(res.get("code")) != "0":
            self.log(f"登录失败: {api_response_err(r, '在线登录')}")
            return False
        self.mobile = str(res.get("desmobile") or "")
        self.ecs_token = str(res.get("ecs_token") or "")
        list_data = res.get("list") if isinstance(res.get("list"), list) else []
        city = list_data[0] if list_data and isinstance(list_data[0], dict) else {}
        self.province = str(city.get("proName") or "")
        code = str(city.get("proCode") or city.get("standardProvinceCode") or "")
        self.province_code = code.lstrip("0") or code
        self.log(f"登录成功: {mask_mobile(self.mobile)} ({self.province})")
        if self.ecs_token:
            self.client.cookies.set("ecs_token", self.ecs_token, domain=".10010.com")
        return True
