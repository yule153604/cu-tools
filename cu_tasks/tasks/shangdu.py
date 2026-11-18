"""商都福利，协议依据 2026-10-31 小程序 HAR。"""

import asyncio
import hashlib
import json
import random
import string
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from gmssl import sm2

from cu_tasks.base import Task
from cu_tasks.common import require_int, safe_int

BASE = "https://app.shangdu.com"
UA = (
    "ChinaUnicom4.x/13.1 (com.chinaunicom.mobilebusiness; build:14; iOS 27.0.0) "
    "Alamofire/4.7.3 unicom{version:iphone_c@13.0100}"
)
APP_ID = "edop_unicom_4b80047a"
CHANNEL = "KSTD"
SIGN_SECRET = "Hwsm6r3afijk8CP9d5F4nZu9DDyeMKMJxRpUqJqxCZ4sZuiyRU"
SM2_PRIVATE_KEY = "03659DDCBCA7513BDD930D4E19B8C216ECB714F1A70ACA2C155BCDD5886283F5"
LOTTERY_PRIZES = (
    "5元话费券",
    "印鸽魔哒熊 电动牙刷",
    "DIY掌中宝照片书",
    "免费领麻布风手提包",
    "免费领定制抱枕套",
    "联通云盘 黄金会员-月卡",
    "爱奇艺黄金VIP会员-月卡",
    "腾讯视频VIP会员-月卡",
)
LOTTERY_FESTIVAL_PRIZES = ("30元话费券", *LOTTERY_PRIZES)
REWARD_DAYS = (14, 7)  # 优先抢高面额 14 天节点（5 元券），再抢 7 天节点（2 元券）
MONTHLY_SIGN_CAP = 14
CLAIM_HOURS = (9, 11, 15, 19)
CLAIM_WAIT_MAX_SECONDS = 180
CLAIM_PREWARM_LEAD_SECONDS = 6.0
CLAIM_OPEN_BUFFER_SECONDS = 0.02
CLAIM_BUSY_RETRIES = 3
CLAIM_BUSY_WAIT_SECONDS = 0.25

_SM2 = None


def sm2_cipher():
    """SM2 解密器无状态，进程内复用一个实例。"""
    global _SM2
    if _SM2 is None:
        _SM2 = sm2.CryptSM2(private_key=SM2_PRIVATE_KEY, public_key="")
    return _SM2


class ShangduTask(Task):
    """商都福利（河南）：签到、签到节点券、抽奖。"""

    name, label = "shangdu", "商都福利"
    provinces = ("河南",)
    claim_only = False

    def __init__(self, account, settings=None):
        super().__init__(account, settings)
        self.access_token = ""
        self._claim_dirty = False

    async def run(self):
        await self.execute(claim_only=self.claim_only)

    @staticmethod
    def sorted_json(data):
        """对齐前端 request.f6956a2d.js 的 It(): 去空值后按 key 排序 JSON.stringify"""
        if not data:
            return "{}"

        def clean(v):
            if isinstance(v, dict):
                return {k: clean(v[k]) for k in sorted(v) if v[k] not in (None, "")}
            if isinstance(v, list):
                return [clean(x) for x in v if x not in (None, "")]
            return v

        return json.dumps(clean(data), ensure_ascii=False, separators=(",", ":"))

    def decrypt(self, raw):
        """解密 /unicomapp/* 返回的 SM2 密文（HAR: request.f6956a2d.js）"""
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw
        text = str(raw).strip()
        if not text:
            return None
        if text[0] in "{[":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
        text = text.strip('"')
        if not text.startswith("04"):
            return {"raw": text}
        try:
            plain = sm2_cipher().decrypt(bytes.fromhex(text[2:].lower()))
            if not isinstance(plain, (bytes, bytearray)) or not plain:
                raise ValueError(f"SM2解密返回非法类型或空值: {type(plain).__name__}")
            return json.loads(plain.decode("utf-8"))
        except Exception as e:
            self.tlog_exc(e, "SM2解密")
            return {"_cipher": text}

    def headers(self, method, path, params=None, data=None):
        """生成 APPLET 接口签名头；签名算法与新 HAR 校验一致。"""
        request_id = "".join(
            random.choices(string.ascii_lowercase + string.digits, k=11)
        )
        ts = str(int(time.time() * 1000))

        merged = {}
        if "?" in path:
            merged.update({k: v[-1] for k, v in parse_qs(urlparse(path).query).items()})
        if params:
            merged.update(params)
        if data:
            merged.update(data)

        body = self.sorted_json(merged)
        sign_raw = f"{SIGN_SECRET}{request_id}{ts}{body}"
        token = self.access_token
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": UA,
            "X-Client-Type": "APPLET",
            "Content-Type": (
                "application/json"
                if method.upper() == "POST"
                else "application/x-www-form-urlencoded"
            ),
            "timestamp": ts,
            "requestId": request_id,
            "signature": hashlib.md5(sign_raw.encode()).hexdigest().upper(),
        }
        if token:
            headers["Authorization"] = token
        return headers

    async def login(self):
        """ecs_token → 小程序 ticket → loginAppAppletFree → Authorization。"""
        self.access_token = ""
        ticket = await self.edop_ticket(
            APP_ID,
            headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if not ticket:
            return False
        login = await self.api(
            "POST",
            "/auth/loginAppAppletFree",
            data={"ticket": ticket, "channel": CHANNEL},
        )
        if not self.api_ok(login):
            self.tlog(f"商都小程序登录失败: {self.api_msg(login)}")
            return False
        data = login["data"].get("data")
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            self.tlog("商都小程序登录未返回有效 token")
            return False
        self.access_token = token
        return True

    async def api(self, method, path, params=None, data=None):
        """调用 /unicomapp/* 并解密 SM2 响应"""
        url = f"{BASE}/unicomapp{path}"
        headers = self.headers(method, path, params, data)
        r = await self.req(
            method,
            url,
            headers=headers,
            params=params or None,
            json=(data if data is not None else {})
            if method.upper() == "POST"
            else None,
        )
        payload = self.decrypt(r.get("data"))
        return {
            "code": r.get("code", -1),
            "headers": r.get("headers", {}),
            "data": payload,
            "error": r.get("error"),
        }

    async def click_log(self, event_code, event_name, module, seq_no, url=""):
        return await self.api(
            "GET",
            "/activityArea/recordClickOperationLog",
            params={
                "pageName": "MonthRechargeWelfare",
                "activeName": "福利月月冲",
                "eventType": "CLICK",
                "pageSeq": "1",
                "seqNo": seq_no,
                "eventCode": event_code,
                "eventName": event_name,
                "module": module,
                "url": url,
            },
        )

    def api_ok(self, resp):
        data = resp.get("data") if isinstance(resp, dict) else None
        return (
            isinstance(resp, dict)
            and resp.get("code") == 200
            and not resp.get("error")
            and isinstance(data, dict)
            and data.get("code") == 200
        )

    def api_msg(self, resp, default=""):
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict):
            return data.get("msg") or data.get("message") or default
        return default

    def lottery_prize_name(self, prize_index, is_festival=False):
        """lottery 接口 data 为转盘奖品下标，非金额（对齐 MonthRechargeWelfare.js）"""
        prizes = LOTTERY_FESTIVAL_PRIZES if is_festival else LOTTERY_PRIZES
        if type(prize_index) is int and 0 <= prize_index < len(prizes):
            return prizes[prize_index]
        return str(prize_index)

    async def fetch_state(self):
        response = await self.api("GET", "/monthlyRecharge/init")
        if not self.api_ok(response):
            raise ValueError(f"商都状态查询失败: {self.api_msg(response)}")
        data = response["data"].get("data")
        if not isinstance(data, dict):
            raise ValueError("商都 init.data 应为对象")
        return data

    @staticmethod
    def next_claim_window(now: datetime | None = None) -> datetime | None:
        now = now or datetime.now()
        for hour in CLAIM_HOURS:
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if target > now:
                return target
        return None

    async def wait_until_claim_session(self, activity) -> tuple[dict, bool]:
        """59 分提前预登录时，开场前数秒预热连接并在整点直接进入抢券。"""
        sign_info = activity.get("signInInfo") or {}
        nodes = sign_info.get("rewardNodes") or []
        if not any(node.get("status") == "CLAIMABLE" for node in nodes):
            return activity, False
        session = sign_info.get("signInRewardSession") or {}
        if session.get("disabled") is False:
            return activity, False
        now = datetime.now()
        target = self.next_claim_window(now)
        if target is None:
            return activity, False
        wait = (target - now).total_seconds()
        if wait <= 0 or wait > CLAIM_WAIT_MAX_SECONDS:
            return activity, False
        self.tlog(f"等待 {target:%H:%M} 放券场次开放 {wait:.0f}秒")
        pre_wait = max(0.0, wait - CLAIM_PREWARM_LEAD_SECONDS)
        if pre_wait > 0:
            await asyncio.sleep(pre_wait)
            # 避开整点洪峰，提前数秒刷新资格并温热 TLS 连接；预热失败仍保留初始资格按时直发
            try:
                activity = await self.fetch_state()
            except ValueError as exc:
                self.tlog(f"开场前状态预热失败，沿用初始资格直发: {exc}")
            else:
                sign_info = activity.get("signInInfo") or {}
                nodes = sign_info.get("rewardNodes") or []
                if not any(node.get("status") == "CLAIMABLE" for node in nodes):
                    return activity, False
                session = sign_info.get("signInRewardSession") or {}
                if session.get("disabled") is False:
                    return activity, True
        remain_wait = min(
            max(0.0, (target - datetime.now()).total_seconds()),
            max(0.0, wait - pre_wait),
        )
        await asyncio.sleep(remain_wait + CLAIM_OPEN_BUFFER_SECONDS)
        return activity, True

    async def claim_rewards(self, activity, *, window_opened=False):
        """仅在场次明确可领（或刚跨越整点放券窗口）时补领；业务码 200 不代表发券成功。"""
        self._claim_dirty = False
        sign_info = activity.get("signInInfo") or {}
        nodes = sign_info.get("rewardNodes") or []
        claimable = [node for node in nodes if node.get("status") == "CLAIMABLE"]
        if not claimable:
            self.tlog("当月签到节点券已领取或尚未达标，无需提交")
            return activity
        session = sign_info.get("signInRewardSession") or {}
        if not window_opened and session.get("disabled") is not False:
            self.tlog(
                "签到节点奖励暂不可领: "
                f"{session.get('displayText') or session.get('status') or '场次状态未知'}，"
                "保留领取资格"
            )
            return activity
        # 按天数降序（14 → 7）优先提交高价值节点；多节点同场可领时按轮次交替重试 BUSY，
        # 避免 14 天节点连续 10s 锁超时耗尽 7 天节点的整点放券窗口。
        pending_days: list[int] = []
        for node in claimable:
            day = require_int(node.get("day"), "shangdu.reward.day", minimum=1)
            if day in REWARD_DAYS and day not in pending_days:
                pending_days.append(day)
        pending_days.sort(reverse=True)
        if not pending_days:
            self.tlog("当月签到节点券已领取或尚未达标，无需提交")
            return activity
        pending_verify: list[tuple[int, object]] = []
        sent_claim = False
        abort_remaining = False
        for retry in range(CLAIM_BUSY_RETRIES + 1):
            if not pending_days or abort_remaining:
                break
            if retry > 0:
                await asyncio.sleep(CLAIM_BUSY_WAIT_SECONDS)
            next_days: list[int] = []
            for day in pending_days:
                response = await self.api(
                    "POST", f"/monthlyRecharge/signIn/reward/{day}/claim"
                )
                sent_claim = True
                if not self.api_ok(response):
                    msg = self.api_msg(response)
                    if (
                        response.get("code") == 200
                        and not response.get("error")
                        and retry < CLAIM_BUSY_RETRIES
                        and any(
                            k in msg
                            for k in ("未开始", "暂未", "倒计时", "繁忙", "稍后")
                        )
                    ):
                        self.tlog(
                            f"{day} 天节点场次切换/繁忙({msg})，"
                            f"重试 {retry + 1}/{CLAIM_BUSY_RETRIES}"
                        )
                        next_days.append(day)
                        continue
                    self.tlog(f"{day} 天节点领取失败: {msg}")
                    abort_remaining = True
                    break
                receipt = response["data"].get("data")
                status = receipt.get("status") if isinstance(receipt, dict) else None
                if status in ("SESSION_EXHAUSTED", "EXHAUSTED"):
                    self.tlog(f"{day} 天节点本场已领完（{status}），本轮不重试该节点")
                    continue
                if status in ("BUSY", "NOT_STARTED"):
                    if retry < CLAIM_BUSY_RETRIES:
                        self.tlog(
                            f"{day} 天节点系统繁忙({status})，"
                            f"重试 {retry + 1}/{CLAIM_BUSY_RETRIES}"
                        )
                        next_days.append(day)
                        continue
                    pending_verify.append((day, status))
                    continue
                if status == "CLAIMED":
                    pending_verify.append((day, status))
                    continue
                # 未知回执状态：不继续提交后续节点，统一读回状态核实
                pending_verify.append((day, status))
                abort_remaining = True
                break
            pending_days = next_days

        if pending_verify:
            pending_verify.sort(key=lambda item: item[0], reverse=True)
            # 领取结果统一由读回的节点状态确认，不凭 HTTP/业务码宣告发券成功。
            activity = await self.fetch_state()
            current = activity.get("signInInfo") or {}
            current_nodes = current.get("rewardNodes") or []
            for day, status in pending_verify:
                state = next(
                    (
                        n.get("status")
                        for n in current_nodes
                        if str(n.get("day")) == str(day)
                    ),
                    None,
                )
                if state == "CLAIMED":
                    self.tlog(f"{day} 天节点奖励领取成功，已读回 CLAIMED 状态")
                elif state == "EXHAUSTED":
                    self.tlog(f"{day} 天节点今日已领完（EXHAUSTED），本轮不重试该节点")
                elif status in ("BUSY", "NOT_STARTED") and state == "CLAIMABLE":
                    self.tlog(
                        f"{day} 天节点系统繁忙({status})未领到，节点状态={state}，不再重试该节点"
                    )
                else:
                    self.tlog(
                        f"{day} 天节点领取待确认: 返回状态={status} 节点状态={state}，不重试"
                    )
        else:
            self._claim_dirty = sent_claim
        return activity

    async def execute(self, *, claim_only=False):
        if not await self.login():
            self.tlog("登录失败")
            return
        self.tlog("登录成功")
        activity = await self.fetch_state()
        if activity.get("isTargetUser") is not True:
            self.tlog("当前账号不符合活动参与条件或资格未知，跳过")
            return
        if claim_only:
            activity, window_opened = await self.wait_until_claim_session(activity)
            await self.claim_rewards(activity, window_opened=window_opened)
            return
        sign_info = activity.get("signInInfo") or {}
        self.tlog(
            f"初始化完成 签到状态={sign_info.get('status')} "
            f"抽奖次数={(activity.get('lotteryInfo') or {}).get('remainCount')}"
        )
        await self.api("GET", "/monthlyRecharge/rollingText")
        status = require_int(
            sign_info.get("status"), "shangdu.signIn.status", minimum=0
        )
        sign_count = safe_int(sign_info.get("signInCount"), 0)
        cap_reached = False
        signed_attempted = False
        if status == 1:
            self.tlog(f"今日已签到(累计{sign_count}天)")
        elif status == 0 and sign_count >= MONTHLY_SIGN_CAP:
            cap_reached = True
            self.tlog(f"当月签到已满(累计{sign_count}天)，跳过签到")
        elif status == 0:
            signed_attempted = True
            response = await self.api("POST", "/monthlyRecharge/signIn")
            await self.click_log("SIGN_CLICK", "签到点击", "signIn", "A")
            if not self.api_ok(response):
                msg = self.api_msg(response)
                # 当月累计满 14 天后服务端返回“当月签到次数已达上线”，仍需继续执行领券与抽奖
                if any(k in msg for k in ("已达上线", "已达上限")):
                    cap_reached = True
                    self.tlog(f"当月签到已满(累计{sign_count}天): {msg}")
                else:
                    self.tlog(f"签到失败或结果不确定: {msg}，停止本轮")
                    return
        else:
            self.tlog(f"签到状态未知: {status}，停止本轮")
            return
        # 发起过签到或存在待抽奖次数时刷新状态；查询失败时不能沿用旧次数继续提交。
        initial_remain = safe_int(
            (activity.get("lotteryInfo") or {}).get("remainCount"), 0
        )
        if signed_attempted or initial_remain > 0:
            activity = await self.fetch_state()
        if signed_attempted and not cap_reached:
            current_sign = activity.get("signInInfo") or {}
            if current_sign.get("status") != 1:
                self.tlog("签到状态尚未确认，停止本轮，不重复提交")
                return
            self.tlog("签到成功，已读回今日签到状态")
        activity = await self.claim_rewards(activity)
        if self._claim_dirty:
            activity = await self.fetch_state()
        lottery_info = activity.get("lotteryInfo") or {}
        remain = require_int(
            lottery_info.get("remainCount"), "shangdu.remainCount", minimum=0
        )
        festival_flag = lottery_info.get("isFestival")
        festival = festival_flag is True or festival_flag == "true"
        # 新规则为每天 1 次，节气回馈日额外 1 次；用户上限只可进一步收紧。
        daily_limit = 2 if festival else 1
        user_max = self.settings.shangdu_lottery_max
        configured = daily_limit if user_max is None else user_max
        budget = min(remain, daily_limit, configured)
        if budget <= 0:
            self.tlog("无可用抽奖次数或抽奖上限为 0")
            return
        for attempt in range(budget):
            await asyncio.sleep(1)
            response = await self.api("POST", "/monthlyRecharge/lottery")
            await self.click_log("LOTTERY_CLICK", "抽奖点击", "lottery", "C")
            if not self.api_ok(response):
                self.tlog(
                    f"第{attempt + 1}次抽奖失败或结果不确定: "
                    f"{self.api_msg(response)}，停止提交"
                )
                return
            prize_index = response["data"].get("data")
            prize_name = self.lottery_prize_name(prize_index, festival)
            self.tlog(f"第{attempt + 1}次抽奖成功 奖品=[{prize_name}]")
            activity = await self.fetch_state()
            lottery_info = activity.get("lotteryInfo") or {}
            remaining = require_int(
                lottery_info.get("remainCount"), "shangdu.remainCount", minimum=0
            )
            if remaining >= remain:
                self.tlog("抽奖次数未减少，停止本轮，不重复提交")
                return
            if remaining == 0:
                return
            remain = remaining


class ShangduClaimTask(ShangduTask):
    """仅领取签到节点券（09/11/15/19 点放券），不签到、不抽奖。"""

    name, label = "shangdu_claim", "商都节点券"
    claim_only = True
