"""新疆联通每月主题活动：每月抽奖 + 每日打卡抽奖。"""

import asyncio
from datetime import datetime

from cu_tasks.base import Task
from cu_tasks.session import UA

BASE = "https://zy100.xj169.com/touchpoint/openapi"
ENTRY = f"{BASE}/jumpHandRoom1G?source=155&type=02"
# 页面上有两个打卡按钮，逐个尝试
DAKA_PRIZES = ("daka_sftczxqb_twenty", "daka_mtwmhblly")
MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def activity_suffix(now=None):
    """10 月新版：接口后缀为 Oct2026Act，主题活动 ID 带 Theme 前缀。"""
    now = now or datetime.now()
    return f"{MONTH_ABBR[now.month - 1]}{now.year}Act"


def is_success(res):
    return res.get("code") in (0, "SUCCESS")


class XinjiangTask(Task):
    name, label = "xj", "新疆联通"
    provinces = ("新疆",)

    referer = token = ""

    def headers(self, with_token=True):
        h = {
            "Referer": self.referer,
            "Origin": "https://zy100.xj169.com",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        }
        if with_token:
            h["userToken"] = self.token
        return h

    async def post(self, path, data, with_token=True):
        r = await self.req(
            "POST",
            f"{BASE}/{path}",
            headers=self.headers(with_token),
            data=data,
        )
        return r["data"] if isinstance(r["data"], dict) else None

    async def login(self):
        ticket = await self.get_ticket(ENTRY)
        if not ticket:
            self.tlog("获取 ticket 失败")
            return False
        # 9 月改版后 draw 接口有会话校验：必须先访问落地页让服务端下发会话 cookie，
        # 并用完整落地页 URL 作 Referer
        if self.last_ticket_url:
            await self.req("GET", self.last_ticket_url, headers={"User-Agent": UA})
        self.referer = self.last_ticket_url or f"{ENTRY}&ticket={ticket}"
        res = await self.post("getTokenAndCity", {"ticket": ticket}, with_token=False)
        data = res.get("data") if res and res.get("code") == 0 else None
        self.token = (data or {}).get("token") if isinstance(data, dict) else ""
        if not self.token:
            self.tlog("登录失败")
            return False
        self.tlog("登录成功")
        return True

    async def comm_high_flag(self, activity_id):
        """服务端要求小写字符串 true/false。"""
        res = await self.post("themeAct/commonHightRest", {"activityId": activity_id})
        if res and is_success(res):
            raw = res.get("data", False)
            if raw is True or str(raw).lower() == "true":
                return "true"
        return "false"

    async def monthly_draw(self, suffix, activity_id, flag):
        res = await self.post(
            f"themeAct/draw_{suffix}",
            {"activityId": activity_id, "prizeId": "", "commHighFlag": flag},
        )
        if not res:
            return
        msg, data = res.get("msg"), res.get("data")
        if msg == "thanks1":
            self.tlog(f"每月抽奖: {data}")
            return
        if not is_success(res):
            self.tlog(f"每月抽奖: {data or msg}")
            return
        self.tlog(f"每月抽奖: {msg or data}")
        # 中奖后与活动页一致：下发短信
        if msg:
            await self.post(
                "marchAct/draw_SMS", {"activityId": activity_id, "prizeId": str(msg)}
            )

    async def daka_draw(self, n, suffix, activity_id, prize, flag):
        res = await self.post(
            f"themeAct/draw_{suffix}",
            {
                "activityId": f"daka{activity_id}",
                "prizeId": prize,
                "commHighFlag": flag,
            },
        )
        if not res:
            return
        msg, data = res.get("msg"), res.get("data")
        if msg == "thanks1":
            self.tlog(f"每日打卡{n}: {data}")
        elif is_success(res):
            self.tlog(f"每日打卡{n}: {msg or data}")
        elif res.get("msgType") == "200":
            self.tlog(f"每日打卡{n}: 已打过卡")
        else:
            self.tlog(f"每日打卡{n}: {data or msg}")

    async def run(self):
        if not await self.login():
            return
        suffix = activity_suffix()
        activity_id = f"Theme{suffix}"
        flag = await self.comm_high_flag(activity_id)
        await self.monthly_draw(suffix, activity_id, flag)
        for n, prize in enumerate(DAKA_PRIZES, start=1):
            await asyncio.sleep(1)
            await self.daka_draw(n, suffix, activity_id, prize, flag)
