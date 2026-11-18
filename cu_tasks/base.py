"""活动任务基类：每个任务持有自己的状态、客户端与日志，经 Account 共享登录态。"""

from typing import ClassVar
from urllib.parse import parse_qs, urlparse

from cu_tasks.common import api_response_err, format_exception_detail
from cu_tasks.config import Settings
from cu_tasks.net import request
from cu_tasks.session import format_line

OPEN_PLAT_URL = (
    "https://m.client.10010.com/mobileService/openPlatform/openPlatLineNew.htm"
)
EDOP_TICKET_URL = "https://m.client.10010.com/edop_ng/getTicketByNative"


class Task:
    """子类声明 name/label 并实现 run()；provinces 非空时仅对这些归属地执行。"""

    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    provinces: ClassVar[tuple[str, ...]] = ()

    def __init__(self, account, settings: Settings | None = None):
        self.account = account
        self.settings = settings or Settings()
        self.client = account.new_client()
        self.lines: list[str] = []
        self.last_ticket_url = ""

    async def run(self) -> None:
        raise NotImplementedError

    def applicable(self) -> bool:
        return not self.provinces or any(p in self.province for p in self.provinces)

    async def close(self):
        await self.client.aclose()

    # --- 账号信息（只读代理）---
    @property
    def idx(self):
        return self.account.idx

    @property
    def mobile(self):
        return self.account.mobile

    @property
    def province(self):
        return self.account.province

    @property
    def ecs_token(self):
        return self.account.ecs_token

    @property
    def uuid(self):
        return self.account.uuid

    @property
    def unicom_token_id(self):
        return self.account.unicom_token_id

    # --- 日志 ---
    def tlog(self, msg):
        self.account.emit(
            format_line(self.idx, msg, self.label, mobile=self.mobile), self.lines
        )

    def tlog_exc(self, e, action=None):
        self.tlog(f"异常: {format_exception_detail(e, action, mobile=self.mobile)}")

    # --- 请求与换票 ---
    async def req(self, method, url, **kw):
        return await request(self.client, method, url, **kw)

    async def get_ticket(self, url) -> str:
        """openPlatLineNew 跳转换 ticket；落地页完整 URL 存于 last_ticket_url。"""
        r = await self.req(
            "GET", OPEN_PLAT_URL, params={"to_url": url}, allow_redirects=False
        )
        loc = r["headers"].get("location") or r["headers"].get("Location")
        if not loc:
            return ""
        self.last_ticket_url = loc
        return parse_qs(urlparse(loc).query).get("ticket", [""])[0]

    async def edop_ticket(self, app_id, *, headers=None, action="getTicketByNative"):
        """ecs_token 换小程序/合作方 ticket；失败记录原因并返回空串。"""
        if not self.ecs_token:
            self.tlog(f"{action} 跳过: 缺少 ecs_token")
            return ""
        r = await self.req(
            "GET",
            EDOP_TICKET_URL,
            params={"appId": app_id, "token": self.ecs_token},
            headers=headers or {},
        )
        body = r.get("data")
        ticket = body.get("ticket") if isinstance(body, dict) else None
        if (
            r.get("code") != 200
            or r.get("error")
            or not isinstance(body, dict)
            or body.get("rsp_code", "0000") != "0000"
            or not isinstance(ticket, str)
            or not ticket.strip()
        ):
            self.tlog(f"换票失败: {api_response_err(r, action)}")
            return ""
        return ticket
