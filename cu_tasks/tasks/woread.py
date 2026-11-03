"""沃阅读与爱听积分。

链路: ecs_token → app/auth(accesstoken) → account/login(token/verifyCode)
      → getPointCenterTicket(well ticket) → 阅读刷时长 → 积分中心签到/领积分
"""

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import datetime

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from cu_tasks.base import Task
from cu_tasks.common import api_response_err, mask_mobile
from cu_tasks.jftask import JfTaskClient, task_pending

BASE = "https://10010.woread.com.cn/ng_woread_service/rest"
AES_KEY = b"woreadst^&*12345"
AES_IV = b"16-Bytes--String"
APP_ID = "10000002"
APP_SECRET = "7k1HcDL8RKvc"
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko)  unicom{version:iphone_c@12.1400}"
)
JF_PARTNERS_ID = "1706"
JF_CLIENT_TYPE = "aiting_unicom"
JF_SIGN_TASK = "s818795876839563264"
# 阅读上报使用的固定书目/章节（HAR 实测）
BOOK = {
    "cntindex": 3120941,
    "chapterallindex": 120470473,
    "chapterseno": 3,
    "cntname": "大清权臣李鸿章",
    "chaptertitle": "少年，胸怀壮志",
}
# 2026-08-07 taskRecords 实测：
#   ✅ 登录并读听看4分钟 + 签到 → 真到账
#   ❌ 分享（需真实微信回调，直调假成功）/ 邀请（需真新用户）/ 会员（需付费）
#      / 限时福利（需爱看 App）→ 跳过
SKIP_KEYWORDS = ("邀请", "会员", "分享", "限时福利")


def aes_hex_b64(plain: str) -> str:
    """AES-CBC 加密 → hex → base64（前端 getAes/getAes2）。"""
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
    return base64.b64encode(
        cipher.encrypt(pad(plain.encode(), 16)).hex().encode()
    ).decode()


def sign_body(data: dict) -> dict:
    return {"sign": aes_hex_b64(json.dumps(data, separators=(",", ":")))}


def ts_now() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def ok(r) -> dict | None:
    res = r.get("data")
    return res if isinstance(res, dict) and res.get("code") == "0000" else None


class WoreadTask(Task):
    name, label = "woread", "沃阅读积分"

    def __init__(self, account, settings=None):
        super().__init__(account, settings)
        self.headers = {
            "User-Agent": UA,
            "Content-Type": "application/json;charset=UTF-8",
            "Referer": "https://10010.woread.com.cn/ng_woread/",
            "Origin": "https://10010.woread.com.cn",
        }
        self.user: dict = {}

    async def post(self, path, data, headers=None):
        return await self.req(
            "POST",
            BASE + path,
            headers=headers or self.headers,
            json=sign_body(data),
        )

    async def auth(self) -> bool:
        """① getAccessToken（无需登录态）② accountLogin 换 token/verifyCode。"""
        ts_ms = str(int(time.time() * 1000))
        md5sig = hashlib.md5(f"{APP_ID}{APP_SECRET}{ts_ms}".encode()).hexdigest()
        r = await self.post(
            f"/app/auth/{APP_ID}/{ts_ms}/{md5sig}", {"timestamp": ts_now()}
        )
        token = ((ok(r) or {}).get("data") or {}).get("accesstoken", "")
        if not token:
            self.tlog(f"getAccessToken 失败: {api_response_err(r, 'app/auth')}")
            return False
        self.headers["accesstoken"] = token

        r = await self.post(
            "/account/login",
            {"phone": aes_hex_b64(self.mobile), "timestamp": ts_now()},
            headers={**self.headers, "noPassToken": "true"},
        )
        res = ok(r)
        if res is None:
            self.tlog(f"accountLogin 失败: {api_response_err(r, 'account/login')}")
            return False
        d = res.get("data") or {}
        if not d.get("token") or not d.get("verifycode"):
            missing = ",".join(k for k in ("token", "verifycode") if not d.get(k))
            self.tlog(f"accountLogin 凭证不完整: 缺少 {missing}")
            return False
        self.user = {
            "token": d["token"],
            "userId": d.get("userid", ""),
            "userIndex": d.get("userindex", ""),
            "userAccount": self.mobile,
            "verifyCode": d["verifycode"],
        }
        self.tlog(f"登录成功: {mask_mobile(self.mobile)} 凭证已换")
        return True

    async def well_ticket(self):
        """③ getPointCenterTicket → (落地页 URL, ticket)。"""
        r = await self.post(
            "/activity/getPointCenterTicket", {"timestamp": ts_now(), **self.user}
        )
        url = (ok(r) or {}).get("data") or ""
        match = re.search(r"ticket=([^&]+)", url) if isinstance(url, str) else None
        if not match:
            self.tlog(f"换票失败: {api_response_err(r, 'getPointCenterTicket')}")
            return "", ""
        self.tlog("换票成功")  # ticket 不入日志
        return url, match.group(1)

    async def report_reading(self):
        """readChapter → newreadadd → addReadTime 三步完整复刻。

        新账号必须先读章节建立阅读记录；每次运行上报 2 分钟，服务端约 10-15 分钟
        冷却，多次运行累计完成“读听看4分钟”。readChapter 必须 POST（GET 报
        请求方式不正确，阅读会话不建立，addReadTime 假成功）。
        """
        r = await self.post(
            f"/cnt/readChapter?cntindex={BOOK['cntindex']}"
            f"&chapterallindex={BOOK['chapterallindex']}"
            f"&chapterseno={BOOK['chapterseno']}",
            {
                "chapterSeno": str(BOOK["chapterseno"]),
                "cntIndex": BOOK["cntindex"],
                "beginChapter": 4,
                "timestamp": ts_now(),
                **self.user,
            },
        )
        if res := ok(r):
            self.tlog(f"readChapter 读章节成功 (章节数={len(res.get('list', []))})")
        else:
            self.tlog(f"readChapter: {api_response_err(r, 'readChapter')}")
        await asyncio.sleep(1)

        r = await self.post(
            "/basics/newreadadd",
            {
                "userid": self.user["userId"],
                "cntindex": BOOK["cntindex"],
                "chapterallindex": BOOK["chapterallindex"],
                "cnttype": 1,
                "cntname": BOOK["cntname"],
                "chaptertitle": BOOK["chaptertitle"],
                "readtype": 1,
                "timestamp": ts_now(),
                **self.user,
            },
        )
        if ok(r):
            self.tlog("newreadadd 更新阅读进度成功")
        await asyncio.sleep(1)

        r = await self.post(
            "/history/addReadTime",
            {
                "readTime": 2,
                "cntIndex": BOOK["cntindex"],
                "cntType": 1,
                "cntindex": BOOK["cntindex"],
                "cnttype": 1,
                "chapterallindex": BOOK["chapterallindex"],
                "chapterseno": BOOK["chapterseno"],
                "channelid": "18000688",
                "chapterid": "13120941003",
                "readtype": 1,
                "isend": "0",
                "timestamp": ts_now(),
                **self.user,
            },
        )
        if res := ok(r):
            weektime = (res.get("data") or {}).get("weektime")
            self.tlog(f"阅读上报: +2分钟 (weektime={weektime})")
        else:
            self.tlog(
                f"阅读上报: {api_response_err(r, 'addReadTime')} (冷却中/已满，下次再报)"
            )

    async def claim_points(self, jf: JfTaskClient):
        """④ 爱听签到 ⑤ 领可完成任务积分 ⑥ 查询今日积分。"""
        r = await jf.post("uasptask/sign", {"taskCode": JF_SIGN_TASK})
        if res := ok(r):
            data = res.get("data") or {}
            self.tlog(f"签到成功: {data.get('title', '')} (+{data.get('score', '')})")
        else:
            self.tlog(f"签到: {api_response_err(r, 'sign')} (可能已签)")

        claimed = 0
        for t in await jf.task_list():
            name = t.get("taskName", "")
            if any(k in name for k in SKIP_KEYWORDS) or not task_pending(t):
                continue
            r = await jf.post("jftask/toFinish", {"taskCode": t.get("taskCode")})
            done = isinstance(r.get("data"), dict) and r["data"].get("data") is True
            self.tlog(f"领积分 [{name}]: {'成功' if done else '未达条件'}")
            claimed += done
            await asyncio.sleep(1)

        if (info := await jf.user_info()) is not None:
            self.tlog(
                f"今日积分: {info.get('todayEarnScore')} "
                f"可用: {info.get('availableScore')} 累计: {info.get('allEarnScore')}"
            )
        self.tlog(f"结束: 共领 {claimed} 项积分")

    async def run(self):
        # 种 App 登录态 cookie
        self.client.cookies.set("ecs_token", self.ecs_token, domain=".woread.com.cn")
        self.client.cookies.set("ecs_token", self.ecs_token, domain=".10010.com.cn")
        self.client.cookies.set("u_account", self.mobile, domain=".woread.com.cn")
        if not await self.auth():
            return
        well_url, ticket = await self.well_ticket()
        if not ticket:
            return
        jf = JfTaskClient(
            self,
            ticket=ticket,
            partners_id=JF_PARTNERS_ID,
            client_type=JF_CLIENT_TYPE,
            user_agent=UA,
            extra={"Referer": well_url},
            signed_extra={"Origin": "https://m.jf.10010.com"},
        )
        if not await jf.init():
            return
        await self.report_reading()
        await self.claim_points(jf)
