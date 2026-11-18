"""云手机积分、刮奖与打卡挑战。"""

import asyncio
import hashlib

from cu_tasks.base import Task
from cu_tasks.common import api_response_err, redact_order_id, redact_text, safe_int

H5API = "https://uphone.wostore.cn/h5api"
H5FORPHONE = "https://h5forphone.wostore.cn/h5forphone"
CHECKIN_PAGE = "https://h5forphone.wostore.cn/changXiangVip/CLD13.html"
CHANNEL = "ST-Wode"
CHANNEL_H5 = "ST-Jingang002"
CHANNEL_WINTER = "ST-JGQ"
EDOP_APP_ID = "edop_unicom_68e8fa69"

ACT_SIGN = "Points_Sign_2507"
ACT_OBTAIN = "Points_Obtain_2507"
# 2508 无效；2507 列表无十连商品(2026031010) → 十连暂不可用
ACT_EXCHANGE = "Points_Exchange_2507"
ACT_WINTER = "HD2026092100322"
GOODS_LOTTERY_10 = "2026031010"
OBTAIN_SKIP = frozenset({"012-4", "TASK2026010601"})
# 冬日岁末福利站：任务一(011-2 月卡会员每日1次) + 任务二(N选1，优先 2508-01 去赚积分)
WINTER_VIP_TASK = "011-2"
WINTER_CLAIM_TASK = "2508-01"
WINTER_FALLBACK_LOG_TASKS = (
    ("0514-001", "Uniclaw（龙虾）"),
    ("1001-12", "云存储"),
    ("1001-01", "打开云视频中心"),
    ("1001-08", "打开云游戏中心"),
    ("008-1", "体验全新版AI助手"),
)
WINTER_MAX_DRAWS = 20
POINTS_SETTLE_SECONDS = 1.0
# 打卡挑战赛：需手填 QQ/微信 的奖品类型，跳过自动领取
CHECKIN_SKIP_ACTIVITY_TYPES = frozenset({2, "2"})
APP_UA = (
    "ChinaUnicom4.x/12.13 (com.chinaunicom.mobilebusiness; build:1; iOS 27.0.0) "
    "Alamofire/4.7.3 unicom{version:iphone_c@12.1300}"
)
H5_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko)  unicom{version:iphone_c@12.1300};ltst;OSVersion/27.0"
)


class UphoneTask(Task):
    """云手机：SSO → 打卡挑战赛 → 积分签到/任务/十连 → 冬日岁末福利站。"""

    name, label = "uphone", "云手机积分"

    def __init__(self, account, settings=None):
        super().__init__(account, settings)
        self.cp_token = self.usr_token = self.device_id = ""
        self._points_dirty = False
        self._sign_points = 0
        self._last_task_list_err = ""

    def biz_ok(self, data):
        """业务成功：data 必须为 dict 且 code 表示成功。"""
        if not isinstance(data, dict):
            return False
        return data.get("code") in (200, "200", 0, "0")

    def biz_data(self, r):
        """取出 req() 的 data 字段；非 dict 时返回 None。"""
        if not isinstance(r, dict):
            return None
        data = r.get("data")
        return data if isinstance(data, dict) else None

    def app_headers(self, extra=None):
        h = {
            "User-Agent": APP_UA,
            "Accept": "*/*",
            "channel": CHANNEL,
            "channelCode": CHANNEL,
            "deviceId": self.device_id or "0",
        }
        if self.cp_token:
            h["Authorization"] = self.cp_token
        if extra:
            h.update(extra)
        return h

    def h5_headers(self, extra=None):
        h = {
            "User-Agent": H5_UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json; charset=UTF-8",
            "Origin": "https://uphone.wostore.cn",
        }
        if self.usr_token:
            h["X-USR-TOKEN"] = self.usr_token
        if extra:
            h.update(extra)
        return h

    def checkin_headers(self):
        """打卡挑战赛（h5forphone CLD13），复用 SSO cpToken。"""
        token = self.cp_token or ""
        return {
            "User-Agent": H5_UA,
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": "https://h5forphone.wostore.cn",
            "Referer": f"{CHECKIN_PAGE}?token={token}",
            "X-Requested-With": "XMLHttpRequest",
        }

    async def checkin_post(self, path, extra_json=None):
        """POST h5forphone/activity/*，body 带 accesstoken=cpToken。"""
        if not self.cp_token:
            return None
        payload = {"accesstoken": self.cp_token}
        if extra_json:
            payload.update(extra_json)
        r = await self.req(
            "POST",
            f"{H5FORPHONE}/{path.lstrip('/')}",
            headers=self.checkin_headers(),
            json=payload,
        )
        return self.biz_data(r)

    async def sso_login(self):
        """ecs_token → ticket → cpToken"""
        self.device_id = hashlib.md5(f"{self.mobile or self.uuid}".encode()).hexdigest()

        ticket = await self.edop_ticket(
            EDOP_APP_ID, headers={"User-Agent": APP_UA, "Accept": "*/*"}
        )
        if not ticket:
            return False

        r = await self.req(
            "POST",
            f"{H5API}/token-service/getTokenByTicket",
            headers=self.app_headers({"Content-Type": "application/json"}),
            json={"channel": CHANNEL, "ticket": ticket},
        )
        body = self.biz_data(r)
        token = body.get("data") if body else None
        if not (
            body
            and self.biz_ok(body)
            and isinstance(token, str)
            and token.startswith("eyJ")
        ):
            self.tlog(f"换 cpToken 失败: {api_response_err(r, 'getTokenByTicket')}")
            return False
        self.cp_token = token
        self.tlog("云手机 SSO 成功")
        return True

    async def activity_login(self, activity_id, channel_id=CHANNEL_H5):
        if not self.cp_token:
            return False
        r = await self.req(
            "POST",
            f"{H5API}/activity-service/user/login",
            headers=self.h5_headers(),
            json={
                "identityType": "cloudPhoneLogin",
                "code": self.cp_token,
                "channelId": channel_id,
                "activityId": activity_id,
                "device": "device",
            },
        )
        body = self.biz_data(r)
        nested = body.get("data") if body else None
        tok = nested.get("user_token") if isinstance(nested, dict) else None
        if not (body and self.biz_ok(body) and tok):
            self.tlog(
                f"活动登录失败({activity_id}): {api_response_err(r, 'user/login')}"
            )
            return False
        self.usr_token = str(tok)
        return True

    async def points_summary(self):
        r = await self.req(
            "GET",
            f"{H5API}/activity-service/points/v1/summary",
            headers=self.h5_headers(),
            params={"activityCode": ACT_EXCHANGE},
        )
        body = self.biz_data(r)
        if not body or not self.biz_ok(body):
            return None
        nested = body.get("data")
        return nested if isinstance(nested, dict) else None

    async def sign(self):
        # sign/history 的 signRecords[0] 是最近一次已签记录（通常为昨天），
        # 拿它判"今日已签"会隔天漏签；直接签到，把"已签"业务响应当正常态
        r = await self.req(
            "POST",
            f"{H5API}/activity-service/points/v1/sign",
            headers=self.h5_headers(),
            json={"activityCode": ACT_SIGN},
        )
        body = self.biz_data(r)
        if body and self.biz_ok(body):
            raw_pts = (
                (body.get("data") or {}).get("score")
                if isinstance(body.get("data"), dict)
                else body.get("data")
            )
            self._points_dirty = True
            self._sign_points = max(safe_int(raw_pts, 0), 5)
            self.tlog(f"签到成功: {body.get('msg') or 'ok'}")
            return True
        msg = str((body or {}).get("msg") or "")
        if any(k in msg for k in ("已签", "签过", "重复")):
            self.tlog(f"今日已签到: {msg}")
            return True
        self.tlog(f"签到失败: {api_response_err(r, 'points/sign')}")
        return False

    async def task_list(self, activity_code):
        self._last_task_list_err = ""
        r = await self.req(
            "POST",
            f"{H5API}/activity-service/user/task/list",
            headers=self.h5_headers(),
            json={"activityCode": activity_code},
        )
        body = self.biz_data(r)
        if not body or not self.biz_ok(body):
            self._last_task_list_err = api_response_err(r, "task/list")
            return [], 0
        task_list = body.get("taskList") or []
        if not isinstance(task_list, list):
            task_list = []
        return task_list, safe_int(body.get("rafflesLeftCount"), 0)

    async def task_logs(self, task_code, detail):
        r = await self.req(
            "POST",
            f"{H5API}/activity-service/user/task/logs",
            headers=self.h5_headers(),
            json={
                "logType": "01",
                "logCode": task_code,
                "logSource": "01",
                "logDetail": detail,
            },
        )
        body = self.biz_data(r)
        if body is None:
            return False, {"msg": api_response_err(r, "task/logs")}
        return self.biz_ok(body), body

    async def raffle_get(self, activity_code, task_code):
        r = await self.req(
            "POST",
            f"{H5API}/activity-service/user/task/raffle/get",
            headers=self.h5_headers(),
            json={"activityCode": activity_code, "taskCode": task_code},
        )
        body = self.biz_data(r)
        if isinstance(body, dict) and body.get("code") in (10301, "10301"):
            if activity_code == ACT_OBTAIN:
                self.tlog(f"领奖受限 {task_code}: {body.get('msg')}")
            return False, body
        if body is None:
            return False, api_response_err(r, "raffle/get")
        return self.biz_ok(body), body

    async def obtain_tasks(self) -> int | None:
        """积分任务：先领存量 → 多轮批量上报并核验领奖（支持观看广告等日内多次任务）"""
        if not await self.activity_login(ACT_OBTAIN):
            return None
        tasks, _ = await self.task_list(ACT_OBTAIN)
        claimed = logged = skipped = earned = 0

        for t in tasks:
            if t.get("status") != "UNCLAIMED":
                continue
            code = str(t.get("taskCode") or "")
            ok, _ = await self.raffle_get(ACT_OBTAIN, code)
            if ok:
                claimed += 1
                earned += max(safe_int(t.get("pointsCount"), 0), 0)
                self.tlog(
                    f"领取积分任务 {code} {t.get('taskName')} +{t.get('pointsCount')}"
                )
            await asyncio.sleep(0.2)

        if claimed > 0:
            tasks, _ = await self.task_list(ACT_OBTAIN)

        stalled_codes: set[str] = set()
        skipped_codes: set[str] = set()
        for _ in range(4):
            reported: list[tuple[str, str, object]] = []
            for t in tasks:
                code = str(t.get("taskCode") or "")
                name = str(t.get("taskName") or code)
                if t.get("status") != "INCOMPLETE" or code in stalled_codes:
                    continue
                if code in OBTAIN_SKIP:
                    if code not in skipped_codes:
                        skipped_codes.add(code)
                        skipped += 1
                    continue
                ok, body = await self.task_logs(code, name)
                if not ok:
                    stalled_codes.add(code)
                    self.tlog(f"任务上报失败 {code}: {(body or {}).get('msg')}")
                    await asyncio.sleep(0.15)
                    continue
                logged += 1
                reported.append((code, name, t.get("pointsCount")))
                await asyncio.sleep(0.15)

            if not reported:
                break

            await asyncio.sleep(0.3)
            tasks, _ = await self.task_list(ACT_OBTAIN)
            by_code = {
                str(x.get("taskCode") or ""): x for x in tasks if isinstance(x, dict)
            }
            round_claimed = 0
            for code, name, pts in reported:
                t2 = by_code.get(code)
                if not t2 or t2.get("status") != "UNCLAIMED":
                    stalled_codes.add(code)
                    self.tlog(
                        f"任务未达可领 {code} status={t2.get('status') if t2 else '?'}"
                    )
                    continue
                ok, _ = await self.raffle_get(ACT_OBTAIN, code)
                if ok:
                    claimed += 1
                    round_claimed += 1
                    earned += max(safe_int(pts, 0), 0)
                    self.tlog(f"完成 {code} {name} +{pts}")
                else:
                    stalled_codes.add(code)
                await asyncio.sleep(0.2)

            if round_claimed == 0:
                break
            tasks, _ = await self.task_list(ACT_OBTAIN)

        need_settle = claimed > 0 or self._points_dirty
        pending_delta = earned + (self._sign_points if self._points_dirty else 0)
        self._points_dirty = False
        self._sign_points = 0
        if need_settle:
            # 签到与 raffle/get 后积分异步入账约有 0.5~1.0s 延迟，等待结算后再读余额
            await asyncio.sleep(POINTS_SETTLE_SECONDS)
        summary = await self.points_summary()
        bal = (summary or {}).get("balanceScoreNum")
        if (
            need_settle
            and summary is not None
            and bal is not None
            and safe_int(bal, 0)
            < self.settings.uphone_lottery_cost
            <= safe_int(bal, 0) + pending_delta
        ):
            await asyncio.sleep(POINTS_SETTLE_SECONDS)
            summary2 = await self.points_summary()
            if summary2 is not None and summary2.get("balanceScoreNum") is not None:
                summary = summary2
                bal = summary.get("balanceScoreNum")
        self.tlog(
            f"积分任务结束 claimed={claimed} logged={logged} skipped={skipped} 余额={bal}"
        )
        return safe_int(bal, 0) if summary is not None and bal is not None else None

    async def lottery10_remain(self):
        r = await self.req(
            "GET",
            f"{H5API}/activity-service/points/v1/exchange/list",
            headers=self.h5_headers(),
            params={
                "activityCode": ACT_EXCHANGE,
                "token": self.cp_token,
            },
        )
        body = self.biz_data(r)
        if not body or not self.biz_ok(body):
            return -1
        goods = body.get("data") or []
        if not isinstance(goods, list):
            return -1
        for g in goods:
            if not isinstance(g, dict):
                continue
            if str(g.get("goodsId")) == GOODS_LOTTERY_10:
                ru = g.get("remainUser")
                return -1 if ru is None else safe_int(ru, 0)
        return -2

    async def lottery_10(self, known_balance: int | None = None):
        """余额≥阈值且有剩余次数则十连抽"""
        if known_balance is None:
            if not await self.activity_login(ACT_SIGN):
                return
            summary = await self.points_summary()
            balance = safe_int((summary or {}).get("balanceScoreNum"), 0)
        else:
            balance = known_balance
        if balance < self.settings.uphone_lottery_cost:
            self.tlog(
                f"积分不足十连 余额={balance} 需≥{self.settings.uphone_lottery_cost}"
            )
            return
        if known_balance is not None and not await self.activity_login(ACT_SIGN):
            return
        remain = await self.lottery10_remain()
        if remain <= 0:
            if remain == -2:
                self.tlog("兑换列表暂无十连商品，跳过")
            elif remain == 0:
                self.tlog("十连次数已用完 remainUser=0")
            else:
                self.tlog("十连次数查询失败, 跳过(状态不明不消耗积分)")
            return
        r = await self.req(
            "POST",
            f"{H5API}/activity-service/points/v1/lottery",
            headers=self.h5_headers(),
            json={
                "activityCode": ACT_EXCHANGE,
                "goodsId": GOODS_LOTTERY_10,
            },
        )
        body = self.biz_data(r)
        if not body or not self.biz_ok(body):
            self.tlog(f"十连失败: {api_response_err(r, 'points/lottery')}")
            return
        raw_data = body.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        raw_sm = data.get("summary")
        sm = raw_sm if isinstance(raw_sm, dict) else {}
        raw_results = data.get("results")
        results = raw_results if isinstance(raw_results, list) else []
        prizes = [
            x.get("prizeName")
            for x in results
            if isinstance(x, dict) and x.get("isDrawn")
        ]
        prize_text = ",".join(str(p) for p in prizes if p) if prizes else "无"
        self.tlog(
            f"十连抽奖完成 total={sm.get('total')} won={sm.get('won')} 奖品={prize_text}"
        )
        summary = await self.points_summary()
        self.tlog(f"十连后余额={(summary or {}).get('balanceScoreNum')}")

    def _mask_order_id(self, order_id) -> str:
        return redact_order_id(order_id, mobile=self.mobile)

    async def _claim_winter_task(self, act_id: str, task: dict) -> bool:
        code = str(task.get("taskCode") or "")
        name = task.get("taskName") or code
        ok, body = await self.raffle_get(act_id, code)
        if ok:
            self.tlog(f"冬日领次数成功 {code} {name}")
            return True
        if isinstance(body, dict):
            msg = redact_text(
                body.get("msg")
                or api_response_err({"code": 200, "data": body}, "raffle/get"),
                mobile=self.mobile,
            )
            if body.get("code") in (10301, "10301"):
                self.tlog(f"冬日领次数跳过(日限) {code}: {msg}")
                return False
        else:
            msg = redact_text(body or "未知错误", mobile=self.mobile)
        self.tlog(f"冬日领次数失败 {code}: {msg}")
        return False

    async def winter_claim(self):
        """冬日岁末福利站：任务一(011-2 包月会员) + 任务二(N选1，优先 2508-01 去赚积分)。"""
        act_id = self.settings.uphone_activity_id
        if not await self.activity_login(act_id, self.settings.uphone_channel_id):
            return 0
        tasks, initial_left = await self.task_list(act_id)
        if self._last_task_list_err:
            self.tlog(f"冬日任务列表获取失败: {self._last_task_list_err}")
            return 0
        if not tasks and initial_left <= 0:
            self.tlog("冬日活动未开放或无可用任务，跳过")
            return 0

        known_left = initial_left
        claimed = 0
        claimed_draws = 0
        needs_refresh = False
        vip_task = next(
            (
                t
                for t in tasks
                if isinstance(t, dict)
                and str(t.get("taskCode") or "") == WINTER_VIP_TASK
                and t.get("status") == "UNCLAIMED"
            ),
            None,
        )
        if vip_task is not None and await self._claim_winter_task(act_id, vip_task):
            claimed += 1
            claimed_draws += max(safe_int(vip_task.get("rafflesCount"), 1), 1)
            needs_refresh = True

        pool = [
            t
            for t in tasks
            if isinstance(t, dict) and str(t.get("taskCode") or "") != WINTER_VIP_TASK
        ]
        daily_done = any(
            t.get("status") in ("OBTAINED", "COMPLETED")
            and str(t.get("taskType") or "").upper() == "DAILY"
            for t in pool
        )
        if daily_done:
            eff_left = max(known_left, initial_left + claimed_draws)
            self.tlog(f"冬日每日任务今日已领过 rafflesLeft={eff_left}")
        else:
            pick = next(
                (
                    t
                    for t in pool
                    if str(t.get("taskCode") or "") == WINTER_CLAIM_TASK
                    and t.get("status") == "UNCLAIMED"
                ),
                None,
            ) or next(
                (t for t in pool if t.get("status") == "UNCLAIMED"),
                None,
            )
            if pick is None:
                by_code = {str(t.get("taskCode") or ""): t for t in pool}
                for fb_code, fb_detail in WINTER_FALLBACK_LOG_TASKS:
                    if (by_code.get(fb_code) or {}).get("status") != "INCOMPLETE":
                        continue
                    ok_log, _ = await self.task_logs(fb_code, fb_detail)
                    if not ok_log:
                        continue
                    await asyncio.sleep(0.3)
                    tasks2, left_fb = await self.task_list(act_id)
                    if self._last_task_list_err:
                        continue
                    tasks = tasks2
                    known_left = left_fb
                    needs_refresh = known_left < initial_left + claimed_draws
                    pool = [
                        t
                        for t in tasks
                        if isinstance(t, dict)
                        and str(t.get("taskCode") or "") != WINTER_VIP_TASK
                    ]
                    by_code = {str(t.get("taskCode") or ""): t for t in pool}
                    pick = next(
                        (
                            t
                            for t in pool
                            if str(t.get("taskCode") or "") == fb_code
                            and t.get("status") == "UNCLAIMED"
                        ),
                        None,
                    ) or next(
                        (t for t in pool if t.get("status") == "UNCLAIMED"),
                        None,
                    )
                    if pick is not None:
                        break
            if pick is not None:
                if await self._claim_winter_task(act_id, pick):
                    claimed += 1
                    claimed_draws += max(safe_int(pick.get("rafflesCount"), 1), 1)
                    needs_refresh = True
            else:
                eff_left = max(known_left, initial_left + claimed_draws)
                self.tlog(f"冬日无可领任务 rafflesLeft={eff_left}（未达条件，跳过）")

        if claimed <= 0:
            return known_left
        expected_left = initial_left + claimed_draws
        if needs_refresh:
            _, left2 = await self.task_list(act_id)
            if self._last_task_list_err:
                est = max(known_left, expected_left)
                self.tlog(
                    f"冬日刷新次数失败({self._last_task_list_err})，按预计次数={est}继续"
                )
                return est
            known_left = left2
            if known_left < expected_left:
                await asyncio.sleep(0.5)
                _, left3 = await self.task_list(act_id)
                if self._last_task_list_err:
                    est = max(known_left, expected_left)
                    self.tlog(
                        f"冬日刷新次数失败({self._last_task_list_err})，按预计次数={est}继续"
                    )
                    return est
                known_left = left3
        self.tlog(f"冬日抽奖次数 rafflesLeft={initial_left}→{known_left}")
        return known_left

    async def winter_lottery(
        self, max_draws=WINTER_MAX_DRAWS, initial_left: int | None = None
    ):
        if max_draws <= 0:
            return 0
        act_id = self.settings.uphone_activity_id
        if (initial_left is None or initial_left > 0) and not self.usr_token:
            if not await self.activity_login(act_id, self.settings.uphone_channel_id):
                return 0
        draws = 0
        prev_left: int | None = None
        stopped_on_err = False
        while draws < max_draws:
            if draws == 0 and initial_left is not None:
                left = initial_left
            else:
                _, left = await self.task_list(act_id)
                if self._last_task_list_err:
                    stopped_on_err = True
                    self.tlog(f"冬日查询剩余次数失败: {self._last_task_list_err}")
                    break
            if left <= 0:
                break
            if prev_left is not None and left >= prev_left:
                self.tlog(f"冬日抽奖次数未减少({prev_left}→{left})，停止抽奖")
                break
            prev_left = left
            r = await self.req(
                "POST",
                f"{H5API}/activity-service/lottery",
                headers=self.h5_headers(),
                json={"activityCode": act_id},
            )
            body = self.biz_data(r)
            if not body or not self.biz_ok(body):
                stopped_on_err = True
                self.tlog(f"冬日抽奖失败: {api_response_err(r, 'lottery')}")
                break
            prize_type = body.get("prizeType")
            prize_name = str(body.get("prizeName") or "").strip()
            if not isinstance(prize_type, bool) and prize_type in (0, "0"):
                draws += 1
                self.tlog(f"冬日抽奖#{draws}: 未中奖 ({body.get('msg') or '谢谢参与'})")
            elif not isinstance(prize_type, bool) and prize_type in (1, "1", 2, "2"):
                draws += 1
                order_id = self._mask_order_id(body.get("orderId"))
                suffix = f" order={order_id}" if order_id else ""
                label = prize_name or body.get("msg") or "未知奖品"
                self.tlog(f"冬日抽奖#{draws} 中奖: {label} type={prize_type}{suffix}")
            else:
                stopped_on_err = True
                err_msg = redact_text(body.get("msg") or body, mobile=self.mobile)
                self.tlog(f"冬日抽奖异常: prizeType={prize_type!r} msg={err_msg}")
                break
            await asyncio.sleep(0.4)
        if draws == 0:
            if initial_left is None and not stopped_on_err:
                self.tlog("冬日剩余次数为 0，跳过")
        else:
            self.tlog(f"冬日抽奖结束 draws={draws}")
        return draws

    async def checkin_query(self):
        """查询打卡挑战赛状态。"""
        body = await self.checkin_post("activity/querySignInList")
        if not body:
            self.tlog("打卡查询无响应")
            return None
        if str(body.get("code")) == "50020":
            self.tlog("打卡 token 失效(50020)")
            return None
        if not self.biz_ok(body):
            self.tlog(f"打卡查询失败: {body.get('msg') or body}")
            return None
        return body

    async def checkin_raffle(self, order_id, account="", account_type=""):
        """打卡奖品核销 raffleSignIn（字段 activityOrderid 小写 id）。"""
        if not order_id:
            return False
        masked_oid = self._mask_order_id(order_id)
        body = await self.checkin_post(
            "activity/raffleSignIn",
            {
                "activityOrderid": order_id,
                "account": account or "",
                "accountType": account_type or "",
            },
        )
        if body and self.biz_ok(body):
            redir = redact_text(body.get("redirectUrl") or "", mobile=self.mobile)
            self.tlog(
                f"打卡领奖成功 order={masked_oid}"
                + (f" redirect={redir[:80]}" if redir else "")
            )
            return True
        self.tlog(
            f"打卡领奖失败 order={masked_oid}: "
            f"{(body or {}).get('msg') if isinstance(body, dict) else body}"
        )
        return False

    async def checkin_handle_prize(self, body, source="签到"):
        """处理签到/奖品中的 activityOrderId；type=2 需 QQ/微信则跳过。"""
        if not isinstance(body, dict):
            return
        order_id = body.get("activityOrderId") or body.get("activityOrderid")
        if not order_id:
            return
        masked_oid = self._mask_order_id(order_id)
        act_type = body.get("activityType")
        name = body.get("activityName") or body.get("name") or ""
        if act_type in CHECKIN_SKIP_ACTIVITY_TYPES:
            self.tlog(
                f"打卡奖品需手填账号，跳过({source}) type={act_type} "
                f"{name} order={masked_oid}"
            )
            return
        self.tlog(f"打卡中奖({source}) {name or masked_oid} type={act_type}")
        await self.checkin_raffle(order_id)
        await asyncio.sleep(0.3)

    async def checkin_claim_pending(self):
        """我的奖品：自动领取可核销订单（跳过 type=2）。"""
        body = await self.checkin_post("activity/signInRightList")
        if not body or not self.biz_ok(body):
            if body:
                self.tlog(f"打卡奖品列表失败: {body.get('msg') or body}")
            return
        goods = body.get("goodsList") or []
        if not isinstance(goods, list) or not goods:
            self.tlog("打卡奖品列表为空")
            return
        claimed = 0
        for g in goods:
            if not isinstance(g, dict):
                continue
            state = g.get("state")
            # 前端 award：state 1/2 可点；2 多为已发，失败则跳过
            if state not in (1, 2, "1", "2"):
                continue
            act_type = g.get("activityType")
            order_id = g.get("activityOrderId") or g.get("activityOrderid")
            name = g.get("name") or ""
            if not order_id:
                continue
            if act_type in CHECKIN_SKIP_ACTIVITY_TYPES:
                self.tlog(f"打卡待领跳过(需账号) {name} type={act_type}")
                continue
            # state=2 历史已领过的不重复核销，仅补 state=1
            if state not in (1, "1"):
                continue
            self.tlog(f"打卡补领 {name} order={self._mask_order_id(order_id)}")
            if await self.checkin_raffle(order_id):
                claimed += 1
            await asyncio.sleep(0.3)
        self.tlog(f"打卡奖品补领结束 claimed={claimed} total={len(goods)}")

    async def checkin(self):
        """打卡挑战赛 CLD13：query → signIn → 条件 raffle → 奖品补领。

        复用 uphone_sso_login 的 cpToken，无需 activity-service 登录。
        """
        if not self.cp_token:
            self.tlog("打卡跳过: 无 cpToken")
            return

        info = await self.checkin_query()
        if info is None:
            return
        day = info.get("signDayNum")
        left = info.get("surpriseDayNum")
        self.tlog(f"打卡状态 已签{day}天 再签{left}天有惊喜")
        if info.get("refreshFlag") == "refresh":
            self.tlog("打卡完成一轮，开启新周期")

        body = await self.checkin_post("activity/signIn")
        if not body:
            self.tlog("打卡签到无响应")
            return
        code = str(body.get("code", ""))
        msg = body.get("msg") or ""
        if code == "30038":
            self.tlog(f"打卡今日已签: {msg or '今日已签过'}")
        elif self.biz_ok(body):
            self.tlog(f"打卡成功: {msg or 'ok'}")
            await self.checkin_handle_prize(body, source="签到")
            await asyncio.sleep(0.3)
            info2 = await self.checkin_query()
            if info2:
                self.tlog(
                    f"打卡后 已签{info2.get('signDayNum')}天 "
                    f"再签{info2.get('surpriseDayNum')}天有惊喜"
                )
        else:
            self.tlog(f"打卡失败[{code}]: {msg or body}")

        await self.checkin_claim_pending()

    async def run(self):
        if not await self.sso_login():
            return

        # 打卡挑战赛仅需 cpToken
        await self.checkin()

        sign_ready = await self.activity_login(ACT_SIGN)
        if sign_ready:
            await self.sign()
        balance = await self.obtain_tasks()
        if sign_ready or balance is not None:
            await self.lottery_10(known_balance=balance)
        left = await self.winter_claim()
        await self.winter_lottery(initial_left=left)
        self.tlog("结束")
