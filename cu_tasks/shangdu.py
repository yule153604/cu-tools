"""商都福利，协议依据 2026-10-31 小程序 HAR。"""

import asyncio
import hashlib
import json
import random
import string
import time
from urllib.parse import parse_qs, urlparse

from gmssl import sm2

from cu_tasks.common import (
    SHANGDU_APP_ID,
    SHANGDU_BASE,
    SHANGDU_CHANNEL,
    SHANGDU_LOTTERY_FESTIVAL_PRIZES,
    SHANGDU_LOTTERY_MAX,
    SHANGDU_LOTTERY_PRIZES,
    SHANGDU_SIGN_SECRET,
    SHANGDU_SM2_PRIVATE_KEY,
    SHANGDU_UA,
    api_response_err,
    require_int,
)


class ShangduTasks:
    """活动实现，复用宿主的账号会话、请求与日志接口。"""

    def shangdu_token(self):
        """小程序登录返回的当前账号 token。"""
        return getattr(self, "shangdu_access_token", "")

    @staticmethod
    def shangdu_sorted_json(data):
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

    def shangdu_decrypt(self, raw):
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
            plain = sm2.CryptSM2(
                private_key=SHANGDU_SM2_PRIVATE_KEY, public_key=""
            ).decrypt(bytes.fromhex(text[2:].lower()))
            if not isinstance(plain, (bytes, bytearray)) or not plain:
                raise ValueError(f"SM2解密返回非法类型或空值: {type(plain).__name__}")
            return json.loads(plain.decode("utf-8"))
        except Exception as e:
            self.tlog_exc(e, "SM2解密")
            return {"_cipher": text}

    def shangdu_headers(self, method, path, params=None, data=None):
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

        body = self.shangdu_sorted_json(merged)
        sign_raw = f"{SHANGDU_SIGN_SECRET}{request_id}{ts}{body}"
        token = self.shangdu_token()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": SHANGDU_UA,
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

    async def shangdu_login(self):
        """ecs_token → 小程序 ticket → loginAppAppletFree → Authorization。"""
        self.shangdu_access_token = ""
        if not self.ecs_token:
            return False
        response = await self.req(
            "GET",
            "https://m.client.10010.com/edop_ng/getTicketByNative",
            params={"appId": SHANGDU_APP_ID, "token": self.ecs_token},
            headers={
                "User-Agent": SHANGDU_UA,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        body = response.get("data")
        if (
            response.get("code") != 200
            or response.get("error")
            or not isinstance(body, dict)
            or body.get("rsp_code") != "0000"
            or not isinstance(body.get("ticket"), str)
            or not body["ticket"].strip()
        ):
            self.tlog(
                f"商都换票失败: {api_response_err(response, 'getTicketByNative')}"
            )
            return False
        login = await self.shangdu_api(
            "POST",
            "/auth/loginAppAppletFree",
            data={"ticket": body["ticket"], "channel": SHANGDU_CHANNEL},
        )
        if not self.shangdu_api_ok(login):
            self.tlog(f"商都小程序登录失败: {self.shangdu_api_msg(login)}")
            return False
        data = login["data"].get("data")
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            self.tlog("商都小程序登录未返回有效 token")
            return False
        self.shangdu_access_token = token
        return True

    async def shangdu_api(self, method, path, params=None, data=None):
        """调用 /unicomapp/* 并解密 SM2 响应"""
        url = f"{SHANGDU_BASE}/unicomapp{path}"
        headers = self.shangdu_headers(method, path, params, data)
        r = await self.req(
            method,
            url,
            headers=headers,
            params=params or None,
            json=(data if data is not None else {})
            if method.upper() == "POST"
            else None,
        )
        payload = self.shangdu_decrypt(r.get("data"))
        return {
            "code": r.get("code", -1),
            "headers": r.get("headers", {}),
            "data": payload,
            "error": r.get("error"),
        }

    async def shangdu_click_log(self, event_code, event_name, module, seq_no, url=""):
        return await self.shangdu_api(
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

    def shangdu_api_ok(self, resp):
        data = resp.get("data") if isinstance(resp, dict) else None
        return (
            isinstance(resp, dict)
            and resp.get("code") == 200
            and not resp.get("error")
            and isinstance(data, dict)
            and data.get("code") == 200
        )

    def shangdu_api_msg(self, resp, default=""):
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict):
            return data.get("msg") or data.get("message") or default
        return default

    def shangdu_lottery_prize_name(self, prize_index, is_festival=False):
        """lottery 接口 data 为转盘奖品下标，非金额（对齐 MonthRechargeWelfare.js）"""
        prizes = (
            SHANGDU_LOTTERY_FESTIVAL_PRIZES if is_festival else SHANGDU_LOTTERY_PRIZES
        )
        if type(prize_index) is int and 0 <= prize_index < len(prizes):
            return prizes[prize_index]
        return str(prize_index)

    async def shangdu_init(self):
        response = await self.shangdu_api("GET", "/monthlyRecharge/init")
        if not self.shangdu_api_ok(response):
            raise ValueError(f"商都状态查询失败: {self.shangdu_api_msg(response)}")
        data = response["data"].get("data")
        if not isinstance(data, dict):
            raise ValueError("商都 init.data 应为对象")
        return data

    async def shangdu_claim_rewards(self, activity):
        """仅在场次明确可领时补领；业务码 200 不代表发券成功。"""
        sign_info = activity.get("signInInfo") or {}
        nodes = sign_info.get("rewardNodes") or []
        claimable = [node for node in nodes if node.get("status") == "CLAIMABLE"]
        if not claimable:
            self.tlog("当月签到节点券已领取或尚未达标，无需提交")
            return activity
        session = sign_info.get("signInRewardSession") or {}
        if session.get("disabled") is not False:
            self.tlog(
                "签到节点奖励暂不可领: "
                f"{session.get('displayText') or session.get('status') or '场次状态未知'}，"
                "保留领取资格"
            )
            return activity
        attempted = set()
        for node in claimable:
            day = require_int(node.get("day"), "shangdu.reward.day", minimum=1)
            if day not in (7, 14) or day in attempted:
                continue
            attempted.add(day)
            response = await self.shangdu_api(
                "POST", f"/monthlyRecharge/signIn/reward/{day}/claim"
            )
            if not self.shangdu_api_ok(response):
                self.tlog(f"{day} 天节点领取失败: {self.shangdu_api_msg(response)}")
                return activity
            receipt = response["data"].get("data")
            status = receipt.get("status") if isinstance(receipt, dict) else None
            if status == "SESSION_EXHAUSTED":
                self.tlog(
                    f"{day} 天节点本场已领完（SESSION_EXHAUSTED），本轮不重试该节点"
                )
                continue
            # 领取结果由读回的节点状态确认，不凭 HTTP/业务码宣告发券成功。
            activity = await self.shangdu_init()
            current = activity.get("signInInfo") or {}
            state = next(
                (
                    n.get("status")
                    for n in current.get("rewardNodes", [])
                    if str(n.get("day")) == str(day)
                ),
                None,
            )
            if state != "CLAIMED":
                self.tlog(
                    f"{day} 天节点领取待确认: 返回状态={status} 节点状态={state}，不重试"
                )
                return activity
            self.tlog(f"{day} 天节点奖励领取成功，已读回 CLAIMED 状态")
            session = current.get("signInRewardSession") or {}
            if session.get("disabled") is not False:
                return activity
        return activity

    async def shangdu_task(self, *, claim_only=False):
        self._task_tag = "商都福利"
        if "河南" not in self.province:
            self.tlog(f"非河南归属地({self.province})，跳过")
            return
        self.tlog("开始")
        if not await self.shangdu_login():
            self.tlog("登录失败")
            return
        self.tlog("登录成功")
        activity = await self.shangdu_init()
        if activity.get("isTargetUser") is not True:
            self.tlog("当前账号不符合活动参与条件或资格未知，跳过")
            return
        if claim_only:
            await self.shangdu_claim_rewards(activity)
            return
        sign_info = activity.get("signInInfo") or {}
        self.tlog(
            f"初始化完成 签到状态={sign_info.get('status')} "
            f"抽奖次数={(activity.get('lotteryInfo') or {}).get('remainCount')}"
        )
        await self.shangdu_api("GET", "/monthlyRecharge/rollingText")
        status = require_int(
            sign_info.get("status"), "shangdu.signIn.status", minimum=0
        )
        if status == 1:
            self.tlog(f"今日已签到(累计{sign_info.get('signInCount', 0)}天)")
        elif status == 0:
            response = await self.shangdu_api("POST", "/monthlyRecharge/signIn")
            await self.shangdu_click_log("SIGN_CLICK", "签到点击", "signIn", "A")
            if not self.shangdu_api_ok(response):
                self.tlog(
                    f"签到失败或结果不确定: {self.shangdu_api_msg(response)}，停止本轮"
                )
                return
        else:
            self.tlog(f"签到状态未知: {status}，停止本轮")
            return
        # 签到后刷新节点与场次；查询失败时不能沿用旧次数继续提交。
        activity = await self.shangdu_init()
        if status == 0:
            current_sign = activity.get("signInInfo") or {}
            if current_sign.get("status") != 1:
                self.tlog("签到状态尚未确认，停止本轮，不重复提交")
                return
            self.tlog("签到成功，已读回今日签到状态")
        activity = await self.shangdu_claim_rewards(activity)
        activity = await self.shangdu_init()
        lottery_info = activity.get("lotteryInfo") or {}
        remain = require_int(
            lottery_info.get("remainCount"), "shangdu.remainCount", minimum=0
        )
        festival_flag = lottery_info.get("isFestival")
        festival = festival_flag is True or festival_flag == "true"
        # 新规则为每天 1 次，节气回馈日额外 1 次；用户上限只可进一步收紧。
        daily_limit = 2 if festival else 1
        configured = daily_limit if SHANGDU_LOTTERY_MAX is None else SHANGDU_LOTTERY_MAX
        budget = min(remain, daily_limit, configured)
        if budget <= 0:
            self.tlog("无可用抽奖次数或抽奖上限为 0")
            return
        for attempt in range(budget):
            await asyncio.sleep(1)
            response = await self.shangdu_api("POST", "/monthlyRecharge/lottery")
            await self.shangdu_click_log("LOTTERY_CLICK", "抽奖点击", "lottery", "C")
            if not self.shangdu_api_ok(response):
                self.tlog(
                    f"第{attempt + 1}次抽奖失败或结果不确定: "
                    f"{self.shangdu_api_msg(response)}，停止提交"
                )
                return
            prize_index = response["data"].get("data")
            prize_name = self.shangdu_lottery_prize_name(prize_index, festival)
            self.tlog(f"第{attempt + 1}次抽奖成功 奖品=[{prize_name}]")
            activity = await self.shangdu_init()
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
