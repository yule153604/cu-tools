"""新疆联通每月主题活动：每月抽奖 + 每日打卡抽奖。"""

import asyncio
import re
from datetime import datetime
from urllib.parse import urljoin

from cu_tasks.base import Task
from cu_tasks.common import api_response_err
from cu_tasks.session import UA

BASE = "https://zy100.xj169.com/touchpoint/openapi"
ENTRY = f"{BASE}/jumpHandRoom1G?source=155&type=02"
# 页面上有两个二选一打卡按钮，每日仅可打卡一次；首个奖品库存不足时才回退第二个
DAKA_PRIZES = ("daka_sftczxqb_twenty", "daka_mtwmhblly")
# 奖品编码映射（对齐 ThemeNov2026Act_lhj.js 及历月主题活动）
PRIZE_NAMES = {
    "txspVIPhynk": "腾讯视频VIP会员-年卡",
    "wyyyyhjVIPjk": "网易云音乐黑胶VIP-季卡",
    "5Gksjhjhynk": "5G宽视界黄金会员-年卡",
    "ltypzxhynk": "联通云盘尊享会员-年卡",
    "ltypzshyyk": "联通云盘尊享会员-月卡",
    "hmjchyyk": "河马剧场会员-月卡",
    "aqybjVIPhyyk": "爱奇艺白金VIP会员-月卡",
    "hfq_five": "5元话费券（50-5）",
    "hfq_ten": "10元话费券（100-10）",
    "mxbcncqb_tweleve": "蜜雪冰城奶茶券包12元",
    "qsyyVIPyk": "汽水音乐VIP-月卡",
    "ddkcdjq_ten": "滴滴快车10元代金券",
    "ddkcdjq_five": "滴滴快车5元代金券",
    "qycsyxhyyk": "权益超市优享会员-月卡",
    "qycshyyk": "权益超市会员-月卡",
    "spcllxhyyk": "视频彩铃乐享会员-月卡",
    "txspcjysSVIPhyyk": "腾讯视频超级影视SVIP会员-月卡",
    "daka_sftczxqb_twenty": "顺丰同城尊享券包20元",
    "daka_mtwmhblly": "美团外卖红包天天领",
    "thanks1": "谢谢参与",
}
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


_PAGE_ACT_RE = re.compile(r"(Theme[A-Za-z]+\d{4}Act)")
_DAKA_ATTR_RE = re.compile(r'prize_id\s*=\s*["\']([A-Za-z0-9_]+)["\']')
_THEME_JS_RE = re.compile(r'src=["\']([^"\']*Theme[^"\']*\.js[^"\']*)["\']')
_JS_PRIZE_COMMENT_RE = re.compile(
    r"prizeId\s*==\s*['\"]([^'\"]+)['\"]\s*\)\s*\{\s*//\s*([^\r\n]+)"
)


def activity_suffix(now=None):
    """10 月新版：接口后缀为 Oct2026Act，主题活动 ID 带 Theme 前缀。"""
    now = now or datetime.now()
    return f"{MONTH_ABBR[now.month - 1]}{now.year}Act"


def is_success(res):
    return res.get("code") in (0, "SUCCESS")


def prize_label(prize_id, extra_map=None) -> str:
    key = str(prize_id or "").strip()
    if extra_map and key in extra_map:
        return extra_map[key]
    return PRIZE_NAMES.get(key, key)


class XinjiangTask(Task):
    name, label = "xj", "新疆联通"
    provinces = ("新疆",)

    def __init__(self, account, settings=None):
        super().__init__(account, settings)
        self.referer = self.token = ""
        self.page_activity_id = ""
        self.daka_prizes: tuple[str, ...] = ()
        self.page_js_url = ""
        self._js_prizes: dict[str, str] = {}
        self._js_loaded = False

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
        if isinstance(r["data"], dict):
            return r["data"]
        self.tlog(f"请求失败: {api_response_err(r, path)}")
        return None

    def _parse_landing_html(self, html: str, base_url: str):
        act_match = _PAGE_ACT_RE.search(html)
        if act_match:
            self.page_activity_id = act_match.group(1)
        daka_ids = list(dict.fromkeys(_DAKA_ATTR_RE.findall(html)))
        if daka_ids:
            self.daka_prizes = tuple(daka_ids)
        js_match = _THEME_JS_RE.search(html)
        if js_match:
            self.page_js_url = urljoin(base_url, js_match.group(1))

    async def resolve_prize_label(self, prize_id) -> str:
        key = str(prize_id or "").strip()
        if not key or key in PRIZE_NAMES or key in self._js_prizes:
            return prize_label(key, self._js_prizes)
        if self.page_js_url and not self._js_loaded:
            self._js_loaded = True
            r = await self.req(
                "GET",
                self.page_js_url,
                headers={"User-Agent": UA, "Referer": self.referer},
            )
            if r.get("code") == 200 and isinstance(r.get("data"), str):
                for pid, comment in _JS_PRIZE_COMMENT_RE.findall(r["data"]):
                    if comment.strip():
                        self._js_prizes[pid.strip()] = comment.strip()
        return prize_label(key, self._js_prizes)

    async def login(self):
        ticket = await self.get_ticket(ENTRY)
        if not ticket:
            self.tlog("获取 ticket 失败")
            return False
        # 9 月改版后 draw 接口有会话校验：必须先访问落地页让服务端下发会话 cookie，
        # 并用完整落地页 URL 作 Referer；顺带提取页面当前活动 ID 与打卡按钮配置。
        if self.last_ticket_url:
            landing = await self.req(
                "GET", self.last_ticket_url, headers={"User-Agent": UA}
            )
            if landing.get("code") == 200 and isinstance(landing.get("data"), str):
                self._parse_landing_html(landing["data"], self.last_ticket_url)
        self.referer = self.last_ticket_url or f"{ENTRY}&ticket={ticket}"
        res = await self.post("getTokenAndCity", {"ticket": ticket}, with_token=False)
        data = res.get("data") if res and res.get("code") == 0 else None
        self.token = (data or {}).get("token") if isinstance(data, dict) else ""
        if not self.token:
            if res is not None:
                self.tlog(f"登录失败: {res.get('msg') or res.get('code')}")
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

    async def _draw_with_refresh(self, suffix, base_activity_id, payload, flag):
        res = None
        for attempt in range(2):
            res = await self.post(
                f"themeAct/draw_{suffix}",
                {**payload, "commHighFlag": flag},
            )
            if not res:
                return None, flag
            text = f"{res.get('data') or ''} {res.get('msg') or ''}"
            if attempt == 0 and not is_success(res) and "刷新页面重试" in text:
                await asyncio.sleep(1)
                if not await self.login():
                    break
                flag = await self.comm_high_flag(base_activity_id)
                continue
            break
        return res, flag

    async def monthly_draw(self, suffix, activity_id, flag) -> str:
        res, flag = await self._draw_with_refresh(
            suffix,
            activity_id,
            {"activityId": activity_id, "prizeId": ""},
            flag,
        )
        if not res:
            return flag
        msg, data = res.get("msg"), res.get("data")
        if msg == "thanks1":
            self.tlog(f"每月抽奖: {data}")
            return flag
        if not is_success(res):
            self.tlog(f"每月抽奖: {data or msg}")
            return flag
        raw_prize = msg or data
        self.tlog(f"每月抽奖中奖: {await self.resolve_prize_label(raw_prize)}")
        # 中奖后与活动页一致：下发短信（需传原始 prizeId）
        if msg:
            await self.post(
                "marchAct/draw_SMS", {"activityId": activity_id, "prizeId": str(msg)}
            )
        return flag

    async def daka_draw(self, n, suffix, activity_id, prize, flag) -> tuple[str, bool]:
        res, flag = await self._draw_with_refresh(
            suffix,
            activity_id,
            {"activityId": f"daka{activity_id}", "prizeId": prize},
            flag,
        )
        if not res:
            return flag, False
        msg, data = res.get("msg"), res.get("data")
        text = f"{data or ''} {msg or ''}"
        if msg == "thanks1":
            self.tlog(f"每日打卡{n}: {data}")
            return flag, True
        if is_success(res):
            label = await self.resolve_prize_label(msg or data or prize)
            self.tlog(f"每日打卡{n}中奖: {label}")
            return flag, True
        if res.get("msgType") == "200" or any(
            k in text for k in ("只能打卡一次", "已打卡", "已打过卡")
        ):
            self.tlog(f"每日打卡{n}: {data or msg or '已打过卡'}")
            return flag, True
        self.tlog(f"每日打卡{n}: {data or msg}")
        return flag, False

    async def run(self):
        if not await self.login():
            return
        activity_id = self.page_activity_id or f"Theme{activity_suffix()}"
        suffix = activity_id.removeprefix("Theme")
        flag = await self.comm_high_flag(activity_id)
        flag = await self.monthly_draw(suffix, activity_id, flag)
        for n, prize in enumerate(self.daka_prizes or DAKA_PRIZES, start=1):
            await asyncio.sleep(1)
            flag, done = await self.daka_draw(n, suffix, activity_id, prize, flag)
            if done:
                break
