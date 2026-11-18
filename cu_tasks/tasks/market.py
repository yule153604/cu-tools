"""权益超市。"""

import asyncio
import base64
import json
import random
import string
import time
from datetime import datetime

from cu_tasks.base import Task
from cu_tasks.common import (
    api_response_err,
    is_retryable_api_response,
    require_dict_data,
    require_int,
    response_summary,
    safe_int,
)

API = "https://backward.bol.wo.cn/prod-api"
RETRY_BASE_SECONDS = 0.15
PREFETCH_LEAD_SECONDS = 12.0  # 开抢前预拉窗口，配合 45s 连接池保活兼顾容错与温热连接
STAGGER_SECONDS = 0.03
RAFFLE_HARD_LIMIT = 10  # 服务端次数硬上限，防异常值失控
# 领取失败文案含这些词时继续尝试下一商品
TRY_NEXT_KEYWORDS = (
    "已领取",
    "已经抢过",
    "本周",
    "售罄",
    "抢完",
    "已抢空",
    "抢光",
    "库存不足",
    "无库存",
    "已领完",
    "领完",
    "明天十点",
    "明日再来",
)
# 明确不可再领（次数用尽等）时停止
STOP_KEYWORDS = ("今天已经抢过", "今日已领", "没有领取次数", "次数不足")


class MarketTask(Task):
    """权益超市：优享权益 10 点开抢 + 抽奖。"""

    name, label = "market", "权益超市"

    def __init__(self, account, settings=None):
        super().__init__(account, settings)
        self.token = self.login_id = self.cycle_start_time = ""
        self.prefetched_info = self.prefetched_act = None

    def auth_headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def current_date():
        """对齐 HAR：currentTime 不补零，如 2026-7-7"""
        now = datetime.now()
        return f"{now.year}-{now.month}-{now.day}"

    @staticmethod
    def is_open():
        """对齐前端 getDailyTimeRange：每天 10:00~24:00 可领"""
        now = datetime.now()
        return now.hour * 60 + now.minute >= 600

    @staticmethod
    def seconds_until_open(now: datetime | None = None) -> float:
        now = now or datetime.now()
        start = now.replace(hour=10, minute=0, second=0, microsecond=0)
        return max(0.0, (start - now).total_seconds())

    async def req_dict(self, action, method, url, **kw):
        """权益超市请求：要求 data 为 dict；网络/5xx 短重试，失败软返回 None。"""
        retries = self.settings.market_api_retries
        r = None
        for attempt in range(retries):
            r = await self.req(method, url, **kw)
            data = r.get("data") if r else None
            if isinstance(data, dict):
                return data, r
            if attempt + 1 < retries and is_retryable_api_response(r):
                err = api_response_err(r, action)
                self.tlog(f"优享权益: {action} 重试{attempt + 1}/{retries - 1} {err}")
                await asyncio.sleep(RETRY_BASE_SECONDS * (attempt + 1))
                continue
            break
        return None, r

    async def fetch_select_info(self):
        """对齐 HAR：premium 优享会员 vipType=0"""
        res, r = await self.req_dict(
            "selectInfo",
            "POST",
            f"{API}/market/contactVip/selectInfo/v2?vipType=0",
            headers=self.auth_headers(),
            json={},
        )
        if res is None:
            self.tlog(f"优享权益: 会员状态查询失败 {api_response_err(r, 'selectInfo')}")
            return None
        data = res.get("data")
        if res.get("code") != 200 or not data:
            self.tlog(f"优享权益: 会员状态查询失败 {api_response_err(r, 'selectInfo')}")
            return None
        if isinstance(data, list):
            first = data[0]
            if not isinstance(first, dict):
                self.tlog(
                    f"优享权益: selectInfo data[0] 须为 dict，实际为 {type(first).__name__}"
                )
                return None
            info = first
        elif isinstance(data, dict):
            info = data
        else:
            self.tlog(
                f"优享权益: selectInfo data 须为非空 dict 或非空 list，实际为 {type(data).__name__}"
            )
            return None
        try:
            state_ok = require_int(info.get("state"), "selectInfo.state") == 1
        except ValueError as e:
            self.tlog(f"优享权益: 会员状态字段异常 {e}")
            return None
        if not state_ok:
            self.tlog("优享权益: 非有效优享会员，跳过")
            return None
        self.cycle_start_time = str(info.get("cycleStartTime") or "")
        return info

    async def fetch_youchoice_activity(self):
        payload = {
            "majorId": 3,
            "subCodeList": ["COMMUNICATE_GOODS", "YOUCHOICEONE"],
            "currentTime": self.current_date(),
            "withUserStatus": 1,
        }
        if self.cycle_start_time:
            payload["cycleStartTime"] = self.cycle_start_time
        res, r = await self.req_dict(
            "getActivitiesDetail/v2",
            "POST",
            f"{API}/promotion/activity/roll/getActivitiesDetail/v2",
            headers=self.auth_headers(),
            json=payload,
        )
        if res is None:
            self.tlog(
                f"优享权益: 活动查询失败 {api_response_err(r, 'getActivitiesDetail/v2')}"
            )
            return None
        data = res.get("data")
        if res.get("code") != 200 or not data:
            self.tlog(
                f"优享权益: 活动查询失败 {api_response_err(r, 'getActivitiesDetail/v2')}"
            )
            return None
        if not isinstance(data, list):
            self.tlog(
                f"优享权益: getActivitiesDetail/v2 data 须为 list，实际为 {type(data).__name__}"
            )
            return None
        for act in data:
            if not isinstance(act, dict):
                self.tlog(
                    f"优享权益: getActivitiesDetail/v2 data 元素须为 dict，实际为 {type(act).__name__}"
                )
                return None
            if act.get("activityCode") == "YOUCHOICEONE":
                return act
        self.tlog(
            "优享权益: 未找到 YOUCHOICEONE 活动, 不领取兜底第一项 "
            f"(共 {len(data)} 项: {[str(a.get('activityCode')) for a in data][:5]})"
        )
        return None

    def build_try_order(self, detail_list):
        """优先当日惊喜[0]；其后按 n-1..1 倒序尝试基础权益，跳过 status=3。

        惊喜售罄/拦截/失败时仍继续后面的基础商品，不再只试惊喜一项。
        """
        detail_list = detail_list or []
        if not detail_list:
            return []
        order = []
        first = detail_list[0]
        surprise = require_int(first.get("isSurprise"), "isSurprise") == 1
        if surprise:
            if require_int(first.get("status"), "status") != 3:
                order.append(first)
            for idx in range(len(detail_list) - 1, 0, -1):
                item = detail_list[idx]
                if require_int(item.get("status"), "status") == 3:
                    continue
                order.append(item)
            return order
        # 无惊喜位：整表倒序
        for idx in range(len(detail_list) - 1, -1, -1):
            item = detail_list[idx]
            if require_int(item.get("status"), "status") == 3:
                continue
            order.append(item)
        return order

    @staticmethod
    def should_try_next(msg: str) -> bool:
        """领取失败文案是否应继续尝试下一商品。"""
        return bool(msg) and any(k in msg for k in TRY_NEXT_KEYWORDS)

    def item_blocked_reason(self, act, item):
        """对齐前端 handleReceiveRights 的拦截条件"""
        if not self.is_open():
            return "本场活动暂未开启"
        times = require_int(act.get("userAvailableTimes"), "userAvailableTimes")
        left = require_int(item.get("leftQuantity"), "leftQuantity")
        status = require_int(item.get("status"), "status")
        cycle = str(item.get("cycle") or "")
        if times > 0 and left <= 0:
            return "已抢空，明天十点再来"
        if times <= 0:
            if cycle == "WEEK" and status == 2:
                return "本周已领取过该权益"
            return "今天已经抢过啦"
        if times > 0 and status == 2 and left > 0 and cycle == "WEEK":
            return "本周已领取过该权益"
        if status == 3:
            return "本周期已领取一次"
        if status == 2:
            return "已抢到"
        if left <= 0:
            return "已抢空"
        return ""

    async def unlock_surprise(self, act):
        """对齐 HAR openNew：解锁使用 detailList[0]"""
        detail_list = act.get("detailList") or []
        if not detail_list:
            return False
        first = detail_list[0]
        if (
            require_int(act.get("isUnlock"), "isUnlock") == 1
            or require_int(first.get("isSurprise"), "isSurprise") != 1
        ):
            return True
        if not self.mobile:
            self.tlog("优享权益: 缺少手机号，无法解锁惊喜权益")
            return False
        ts = int(time.time() * 1000)
        xbsosjl = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        res, r = await self.req_dict(
            "unlockSurprise",
            "POST",
            f"{API}/promotion/activity/roll/unlock/surpriseInterest"
            f"?xbsosjl={xbsosjl}&timeVerRan={ts}&diceid={self.login_id}",
            headers=self.auth_headers(),
            json={
                "timeVerRan": ts,
                "mobile": self.mobile,
                "id": first.get("id"),
                "activityId": first.get("activityId") or act.get("activityId"),
            },
        )
        if res is None:
            self.tlog(f"优享权益: 惊喜解锁失败 {api_response_err(r, 'unlockSurprise')}")
            return False
        if res.get("code") == 200:
            self.tlog(f"优享权益: 惊喜解锁成功 {res.get('msg', '')}")
            await asyncio.sleep(1.5)
            return True
        self.tlog(
            f"优享权益: 惊喜解锁失败 {res.get('msg') or api_response_err(r, 'unlockSurprise')}"
        )
        return False

    async def receive_rights(self, act, item):
        payload = {
            "channelId": item.get("channelId"),
            "activityId": act.get("activityId"),
            "productId": item.get("id"),
            "productCode": item.get("productCode", ""),
            "currentTime": self.current_date(),
            "accountType": item.get("accountType", "4"),
        }
        r = await self.req(
            "POST",
            f"{API}/promotion/activity/roll/receiveRightsVip",
            headers=self.auth_headers(),
            json=payload,
        )
        return require_dict_data(r, "receiveRightsVip")

    async def man_machine(self, activity_id):
        if not activity_id:
            return False
        r = await self.req(
            "POST",
            f"{API}/promotion/activity/roll/receive/manMachine?activityId={activity_id}",
            headers=self.auth_headers(),
            json={},
        )
        res = require_dict_data(r, "manMachine")
        return res.get("code") == 200

    async def login(self):
        try:
            ticket = await self.get_ticket("https://contact.bol.wo.cn/")
            if not ticket:
                self.tlog("登录失败: 获取ticket为空")
                return False
            r = await self.req(
                "POST",
                f"{API}/auth/marketUnicomLogin?ticket={ticket}&channel=unicomTab",
                headers={"Content-Type": "application/json"},
                json={},
            )
            if (res := r["data"]) and res.get("code") == 200:
                self.token = res.get("data", {}).get("token", "")
                if self.token:
                    # 解析JWT获取login_id
                    try:
                        payload = self.token.split(".")[1]
                        payload += "=" * (-len(payload) % 4)
                        jwt = json.loads(base64.urlsafe_b64decode(payload).decode())
                        self.login_id = jwt.get("loginId", "")
                    except Exception as e:
                        self.tlog_exc(e, "解析权益超市JWT")
                    self.tlog("登录成功")
                    return True
                self.tlog(
                    f"登录失败: 响应无token | {api_response_err(r, '权益超市登录')}"
                )
                return False
            self.tlog(f"登录失败: {api_response_err(r, '权益超市登录')}")
        except Exception as e:
            self.tlog_exc(e, "权益超市登录")
        return False

    async def privilege(self):
        """优享权益（权益天天领）"""
        try:
            if not self.token:
                return
            self.prefetched_info = None
            self.prefetched_act = None
            if not self.is_open():
                wait = self.seconds_until_open()
                if wait > self.settings.market_wait_max:
                    self.tlog(f"未到10点(距离开抢{wait:.0f}秒)，跳过优享权益")
                    return
                self.tlog(f"等待开抢 {wait:.0f}秒")
                pre_wait = max(0.0, wait - PREFETCH_LEAD_SECONDS)
                if pre_wait > 0:
                    await asyncio.sleep(pre_wait)
                # 临近 10 点再预拉，避免过早预拉导致连接池空闲断连及库存陈旧
                self.prefetched_info = await self.fetch_select_info()
                if not self.prefetched_info:
                    return
                self.tlog("优享权益: 开抢前已预拉会员状态 ✅")
                self.prefetched_act = await self.fetch_youchoice_activity()
                if self.prefetched_act:
                    self.tlog("优享权益: 开抢前已预拉活动数据 ✅")
                remain_wait = self.seconds_until_open()
                if remain_wait > 0:
                    await asyncio.sleep(remain_wait)
            if not self.is_open():
                self.tlog("优享权益: 本场活动暂未开启")
                return

            if self.idx > 1 and STAGGER_SECONDS > 0:
                await asyncio.sleep((self.idx - 1) * STAGGER_SECONDS)

            if self.prefetched_info is None:
                if not await self.fetch_select_info():
                    return

            # 10点直发: 用预拉活动数据直接提交抢购
            if self.prefetched_act:
                act = self.prefetched_act
                items = self.build_try_order(act.get("detailList") or [])
                if items:
                    self.tlog(f"优享权益: 10点直发抢购({len(items)}个商品)")
                    for item in items:
                        name = item.get("productName", "")
                        try:
                            res = await self.receive_rights(act, item)
                        except ValueError as e:
                            self.tlog(f"优享权益: [{name}] 直发失败 {e}")
                            continue
                        code = safe_int(res.get("code"), -1)
                        if code == 200:
                            self.tlog(f"优享权益: [{name}] 成功(10点直发)")
                            return
                        if code == -2:
                            self.tlog(
                                f"优享权益: [{name}] 直发需人机验证，尝试 manMachine"
                            )
                            try:
                                if not await self.man_machine(act.get("activityId")):
                                    continue
                                res = await self.receive_rights(act, item)
                            except ValueError as e:
                                self.tlog(f"优享权益: [{name}] 人机/重试失败 {e}")
                                continue
                            if safe_int(res.get("code"), -1) == 200:
                                self.tlog(f"优享权益: [{name}] 成功(10点直发)")
                                return
                        msg = str(res.get("msg", ""))
                        self.tlog(f"优享权益: [{name}] 直发失败 {msg or res}")
                        if any(k in msg for k in STOP_KEYWORDS):
                            return
                        if self.should_try_next(msg):
                            continue
                        break
                    self.tlog("优享权益: 直发未成功, 降级走常规流程")

            act = await self.fetch_youchoice_activity()
            if not act:
                return
            if require_int(act.get("userAvailableTimes"), "userAvailableTimes") <= 0:
                self.tlog("优享权益: 今日已领")
                return

            detail_list = act.get("detailList") or []
            items = self.build_try_order(detail_list)
            if not items:
                self.tlog("优享权益: 无可尝试商品")
                return
            if detail_list:
                first = detail_list[0]
                if (
                    require_int(first.get("isSurprise"), "isSurprise") == 1
                    and require_int(first.get("status"), "status") == 3
                ):
                    self.tlog("优享权益: 当日惊喜本周期已领，按倒序尝试基础权益")
                elif require_int(first.get("isSurprise"), "isSurprise") == 1:
                    base_n = max(0, len(items) - 1)
                    self.tlog(
                        f"优享权益: 优先尝试惊喜 [{first.get('productName', '')}]"
                        f"，失败后可尝试 {base_n} 个基础权益"
                    )
                else:
                    self.tlog(f"优享权益: 按规则尝试 {len(items)} 个商品")

            need_surprise_unlock = (
                require_int(act.get("isUnlock"), "isUnlock") != 1
                and detail_list
                and require_int(detail_list[0].get("isSurprise"), "isSurprise") == 1
            )
            if need_surprise_unlock:
                unlocked = await self.unlock_surprise(act)
                act = await self.fetch_youchoice_activity() or act
                detail_list = act.get("detailList") or detail_list
                items = self.build_try_order(detail_list)
                if not unlocked:
                    # 解锁失败：去掉惊喜项，只领基础权益
                    first = detail_list[0] if detail_list else None
                    if (
                        first
                        and require_int(first.get("isSurprise"), "isSurprise") == 1
                    ):
                        fid = first.get("id")
                        items = [
                            it
                            for it in items
                            if it is not first and it.get("id") != fid
                        ]
                    self.tlog("优享权益: 惊喜解锁失败，改为尝试基础权益")
                if not items:
                    self.tlog("优享权益: 无可尝试商品")
                    return

            for i, item in enumerate(items):
                name = item.get("productName", "")
                reason = self.item_blocked_reason(act, item)
                if reason:
                    self.tlog(f"优享权益: [{name}] 跳过({reason})")
                    continue

                try:
                    res = await self.receive_rights(act, item)
                except ValueError as e:
                    self.tlog(f"优享权益: [{name}] 领取请求失败 {e}")
                    continue
                code = safe_int(res.get("code"), -1)
                if code == 200:
                    self.tlog(f"优享权益: [{name}] 成功")
                    return
                if code == -2:
                    self.tlog(f"优享权益: [{name}] 需人机验证，尝试 manMachine")
                    try:
                        if not await self.man_machine(act.get("activityId")):
                            self.tlog(
                                f"优享权益: [{name}] 人机验证未通过，尝试下一商品"
                            )
                            continue
                        res = await self.receive_rights(act, item)
                    except ValueError as e:
                        self.tlog(f"优享权益: [{name}] 人机/重试失败 {e}")
                        continue
                    if safe_int(res.get("code"), -1) == 200:
                        self.tlog(f"优享权益: [{name}] 成功")
                        return
                msg = str(res.get("msg", ""))
                self.tlog(f"优享权益: [{name}] 失败 {msg or res}")
                if self.should_try_next(msg):
                    if i + 1 < len(items):
                        nxt = items[i + 1].get("productName", "")
                        self.tlog(f"优享权益: 按规则继续下一商品 [{nxt}]")
                    continue
                # 明确不可再领（次数用尽等）时停止
                if any(k in msg for k in STOP_KEYWORDS):
                    break
            else:
                self.tlog("优享权益: 已尝试全部可领商品，均未成功")
        except Exception as e:
            self.tlog_exc(e, "优享权益")

    async def raffle(self):
        """抽奖"""
        try:
            if not self.token:
                return
            r = await self.req(
                "POST",
                f"{API}/promotion/home/raffleActivity/getUserRaffleCount?id=12&channel=unicomTab",
                headers={"Authorization": f"Bearer {self.token}"},
                json={},
            )
            res = require_dict_data(r, "查询抽奖次数")
            if res.get("code") != 200:
                self.tlog(
                    f"查询抽奖次数失败: code={res.get('code')} {response_summary(res)}"
                )
                return
            count = require_int(res.get("data"), "raffleCount", minimum=0)
            self.tlog(f"抽奖次数: {count}")

            for _ in range(min(count, RAFFLE_HARD_LIMIT)):
                await asyncio.sleep(2)
                ts = int(time.time() * 1000)
                r = await self.req(
                    "POST",
                    f"{API}/promotion/home/raffleActivity/userRaffle?id=12&channel=unicomTab&timeVerRan={ts}",
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={},
                )
                res = require_dict_data(r, "抽奖")
                if res.get("code") != 200:
                    self.tlog(
                        f"抽奖失败: code={res.get('code')} {response_summary(res)}"
                    )
                    break
                data = res.get("data", {})
                if data.get("isWinning") and (prize := data.get("prizesName")):
                    self.tlog(f"抽奖: {prize}")
                else:
                    self.tlog("抽奖: 未中奖")
        except Exception as e:
            self.tlog_exc(e, "抽奖")

    async def run(self):
        if await self.login():
            await self.privilege()
            await self.raffle()
