"""云手机积分、刮奖与打卡挑战。"""

import asyncio
import hashlib

from cu_tasks.common import (
    UPHONE_ACT_EXCHANGE,
    UPHONE_ACT_OBTAIN,
    UPHONE_ACT_SIGN,
    UPHONE_ACT_SUMMER,
    UPHONE_APP_UA,
    UPHONE_CHANNEL,
    UPHONE_CHANNEL_H5,
    UPHONE_CHECKIN_PAGE,
    UPHONE_CHECKIN_SKIP_ACTIVITY_TYPES,
    UPHONE_EDOP_APP_ID,
    UPHONE_GOODS_LOTTERY_10,
    UPHONE_H5_UA,
    UPHONE_H5API,
    UPHONE_H5FORPHONE,
    UPHONE_LOTTERY_COST,
    UPHONE_OBTAIN_SKIP,
    UPHONE_SUMMER_CLAIM_TASK,
    api_response_err,
    safe_int,
)


class UphoneTasks:
    """活动实现，复用宿主的账号会话、请求与日志接口。"""

    def uphone_biz_ok(self, data):
        """业务成功：data 必须为 dict 且 code 表示成功。"""
        if not isinstance(data, dict):
            return False
        return data.get("code") in (200, "200", 0, "0")

    def uphone_biz_data(self, r):
        """取出 req() 的 data 字段；非 dict 时返回 None。"""
        if not isinstance(r, dict):
            return None
        data = r.get("data")
        return data if isinstance(data, dict) else None

    def uphone_app_headers(self, extra=None):
        h = {
            "User-Agent": UPHONE_APP_UA,
            "Accept": "*/*",
            "channel": UPHONE_CHANNEL,
            "channelCode": UPHONE_CHANNEL,
            "deviceId": self.uphone_device_id or "0",
        }
        if self.uphone_cp_token:
            h["Authorization"] = self.uphone_cp_token
        if extra:
            h.update(extra)
        return h

    def uphone_h5_headers(self, extra=None):
        h = {
            "User-Agent": UPHONE_H5_UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json; charset=UTF-8",
            "Origin": "https://uphone.wostore.cn",
        }
        if self.uphone_usr_token:
            h["X-USR-TOKEN"] = self.uphone_usr_token
        if extra:
            h.update(extra)
        return h

    def uphone_checkin_headers(self):
        """打卡挑战赛（h5forphone CLD13），复用 SSO cpToken。"""
        token = self.uphone_cp_token or ""
        return {
            "User-Agent": UPHONE_H5_UA,
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": "https://h5forphone.wostore.cn",
            "Referer": f"{UPHONE_CHECKIN_PAGE}?token={token}",
            "X-Requested-With": "XMLHttpRequest",
        }

    async def uphone_checkin_post(self, path, extra_json=None):
        """POST h5forphone/activity/*，body 带 accesstoken=cpToken。"""
        if not self.uphone_cp_token:
            return None
        payload = {"accesstoken": self.uphone_cp_token}
        if extra_json:
            payload.update(extra_json)
        r = await self.req(
            "POST",
            f"{UPHONE_H5FORPHONE}/{path.lstrip('/')}",
            headers=self.uphone_checkin_headers(),
            json=payload,
        )
        return self.uphone_biz_data(r)

    async def uphone_sso_login(self):
        """ecs_token → ticket → cpToken"""
        if not self.ecs_token:
            self.tlog("无 ecs_token")
            return False
        self.uphone_device_id = hashlib.md5(
            f"{self.mobile or self.uuid}".encode()
        ).hexdigest()

        r = await self.req(
            "GET",
            "https://m.client.10010.com/edop_ng/getTicketByNative",
            params={"token": self.ecs_token, "appId": UPHONE_EDOP_APP_ID},
            headers={"User-Agent": UPHONE_APP_UA, "Accept": "*/*"},
        )
        body = self.uphone_biz_data(r)
        ticket = body.get("ticket") if body else None
        if not ticket:
            self.tlog(f"换 ticket 失败: {api_response_err(r, 'getTicketByNative')}")
            return False

        r = await self.req(
            "POST",
            f"{UPHONE_H5API}/token-service/getTokenByTicket",
            headers=self.uphone_app_headers({"Content-Type": "application/json"}),
            json={"channel": UPHONE_CHANNEL, "ticket": ticket},
        )
        body = self.uphone_biz_data(r)
        token = body.get("data") if body else None
        if not (
            body
            and self.uphone_biz_ok(body)
            and isinstance(token, str)
            and token.startswith("eyJ")
        ):
            self.tlog(f"换 cpToken 失败: {api_response_err(r, 'getTokenByTicket')}")
            return False
        self.uphone_cp_token = token
        self.tlog("云手机 SSO 成功")
        return True

    async def uphone_activity_login(self, activity_id):
        if not self.uphone_cp_token:
            return False
        r = await self.req(
            "POST",
            f"{UPHONE_H5API}/activity-service/user/login",
            headers=self.uphone_h5_headers(),
            json={
                "identityType": "cloudPhoneLogin",
                "code": self.uphone_cp_token,
                "channelId": UPHONE_CHANNEL_H5,
                "activityId": activity_id,
                "device": "device",
            },
        )
        body = self.uphone_biz_data(r)
        nested = body.get("data") if body else None
        tok = nested.get("user_token") if isinstance(nested, dict) else None
        if not (body and self.uphone_biz_ok(body) and tok):
            self.tlog(
                f"活动登录失败({activity_id}): {api_response_err(r, 'user/login')}"
            )
            return False
        self.uphone_usr_token = str(tok)
        return True

    async def uphone_points_summary(self):
        r = await self.req(
            "GET",
            f"{UPHONE_H5API}/activity-service/points/v1/summary",
            headers=self.uphone_h5_headers(),
            params={"activityCode": UPHONE_ACT_EXCHANGE},
        )
        body = self.uphone_biz_data(r)
        if not body or not self.uphone_biz_ok(body):
            return None
        nested = body.get("data")
        return nested if isinstance(nested, dict) else None

    async def uphone_sign(self):
        # sign/history 的 signRecords[0] 是最近一次已签记录（通常为昨天），
        # 拿它判"今日已签"会隔天漏签；直接签到，把"已签"业务响应当正常态
        r = await self.req(
            "POST",
            f"{UPHONE_H5API}/activity-service/points/v1/sign",
            headers=self.uphone_h5_headers(),
            json={"activityCode": UPHONE_ACT_SIGN},
        )
        body = self.uphone_biz_data(r)
        if body and self.uphone_biz_ok(body):
            self.tlog(f"签到成功: {body.get('msg') or 'ok'}")
            return True
        msg = str((body or {}).get("msg") or "")
        if any(k in msg for k in ("已签", "签过", "重复")):
            self.tlog(f"今日已签到: {msg}")
            return True
        self.tlog(f"签到失败: {api_response_err(r, 'points/sign')}")
        return False

    async def uphone_task_list(self, activity_code):
        r = await self.req(
            "POST",
            f"{UPHONE_H5API}/activity-service/user/task/list",
            headers=self.uphone_h5_headers(),
            json={"activityCode": activity_code},
        )
        body = self.uphone_biz_data(r)
        if not body or not self.uphone_biz_ok(body):
            return [], 0
        task_list = body.get("taskList") or []
        if not isinstance(task_list, list):
            task_list = []
        return task_list, safe_int(body.get("rafflesLeftCount"), 0)

    async def uphone_task_logs(self, task_code, detail):
        r = await self.req(
            "POST",
            f"{UPHONE_H5API}/activity-service/user/task/logs",
            headers=self.uphone_h5_headers(),
            json={
                "logType": "01",
                "logCode": task_code,
                "logSource": "01",
                "logDetail": detail,
            },
        )
        body = self.uphone_biz_data(r)
        return self.uphone_biz_ok(body), body

    async def uphone_raffle_get(self, activity_code, task_code):
        r = await self.req(
            "POST",
            f"{UPHONE_H5API}/activity-service/user/task/raffle/get",
            headers=self.uphone_h5_headers(),
            json={"activityCode": activity_code, "taskCode": task_code},
        )
        body = self.uphone_biz_data(r)
        if isinstance(body, dict) and body.get("code") in (10301, "10301"):
            self.tlog(f"领奖受限 {task_code}: {body.get('msg')}")
            return False, body
        return self.uphone_biz_ok(body), body

    async def uphone_obtain_tasks(self):
        """积分任务：logs → 领奖（跳过需真实行为的任务）"""
        if not await self.uphone_activity_login(UPHONE_ACT_OBTAIN):
            return
        tasks, _ = await self.uphone_task_list(UPHONE_ACT_OBTAIN)
        claimed = logged = skipped = 0

        for t in tasks:
            if t.get("status") != "UNCLAIMED":
                continue
            code = str(t.get("taskCode") or "")
            ok, _ = await self.uphone_raffle_get(UPHONE_ACT_OBTAIN, code)
            if ok:
                claimed += 1
                self.tlog(
                    f"领取积分任务 {code} {t.get('taskName')} +{t.get('pointsCount')}"
                )
            await asyncio.sleep(0.25)

        tasks, _ = await self.uphone_task_list(UPHONE_ACT_OBTAIN)
        for t in tasks:
            code = str(t.get("taskCode") or "")
            name = str(t.get("taskName") or code)
            if t.get("status") != "INCOMPLETE":
                continue
            if code in UPHONE_OBTAIN_SKIP:
                skipped += 1
                continue
            ok, body = await self.uphone_task_logs(code, name)
            if not ok:
                self.tlog(f"任务上报失败 {code}: {(body or {}).get('msg')}")
                await asyncio.sleep(0.2)
                continue
            logged += 1
            await asyncio.sleep(0.3)
            tasks2, _ = await self.uphone_task_list(UPHONE_ACT_OBTAIN)
            t2 = next((x for x in tasks2 if x.get("taskCode") == code), None)
            if not t2 or t2.get("status") != "UNCLAIMED":
                self.tlog(
                    f"任务未达可领 {code} status={t2.get('status') if t2 else '?'}"
                )
                continue
            ok, _ = await self.uphone_raffle_get(UPHONE_ACT_OBTAIN, code)
            if ok:
                claimed += 1
                self.tlog(f"完成 {code} {name} +{t.get('pointsCount')}")
            await asyncio.sleep(0.25)

        summary = await self.uphone_points_summary()
        bal = (summary or {}).get("balanceScoreNum")
        self.tlog(
            f"积分任务结束 claimed={claimed} logged={logged} skipped={skipped} 余额={bal}"
        )

    async def uphone_lottery10_remain(self):
        r = await self.req(
            "GET",
            f"{UPHONE_H5API}/activity-service/points/v1/exchange/list",
            headers=self.uphone_h5_headers(),
            params={
                "activityCode": UPHONE_ACT_EXCHANGE,
                "token": self.uphone_cp_token,
            },
        )
        body = self.uphone_biz_data(r)
        if not body or not self.uphone_biz_ok(body):
            return -1
        goods = body.get("data") or []
        if not isinstance(goods, list):
            return -1
        # 打印全部商品 ID 便于排查 (2026-08-06: 十连商品可能不在列表)
        ids = [str(g.get("goodsId")) for g in goods if isinstance(g, dict)]
        self.tlog(f"兑换商品列表 goodsId={ids}")
        for g in goods:
            if isinstance(g, dict):
                self.tlog(
                    f"商品: id={g.get('goodsId')} name={str(g.get('goodsName'))[:40]} remain={g.get('remainUser')} price={g.get('score') or g.get('price')}"
                )
        for g in goods:
            if not isinstance(g, dict):
                continue
            if str(g.get("goodsId")) == UPHONE_GOODS_LOTTERY_10:
                ru = g.get("remainUser")
                # 打印完整字段便于排查 (2026-08-06: 余额200+但remainUser=0)
                self.tlog(f"十连商品完整字段: {str(g)[:300]}")
                return -1 if ru is None else safe_int(ru, 0)
        return 0

    async def uphone_lottery_10(self):
        """余额≥阈值且有剩余次数则十连抽"""
        await self.uphone_activity_login(UPHONE_ACT_SIGN)
        summary = await self.uphone_points_summary()
        balance = safe_int((summary or {}).get("balanceScoreNum"), 0)
        if balance < UPHONE_LOTTERY_COST:
            self.tlog(f"积分不足十连 余额={balance} 需≥{UPHONE_LOTTERY_COST}")
            return
        remain = await self.uphone_lottery10_remain()
        if remain <= 0:
            self.tlog(
                "十连次数已用完 remainUser=0"
                if remain == 0
                else "十连次数查询失败, 跳过(状态不明不消耗积分)"
            )
            return
        r = await self.req(
            "POST",
            f"{UPHONE_H5API}/activity-service/points/v1/lottery",
            headers=self.uphone_h5_headers(),
            json={
                "activityCode": UPHONE_ACT_EXCHANGE,
                "goodsId": UPHONE_GOODS_LOTTERY_10,
            },
        )
        body = self.uphone_biz_data(r)
        if not body or not self.uphone_biz_ok(body):
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
            f"十连完成 total={sm.get('total')} won={sm.get('won')} 奖品={prize_text}"
        )
        summary = await self.uphone_points_summary()
        self.tlog(f"十连后余额={(summary or {}).get('balanceScoreNum')}")

    async def uphone_summer_claim_one(self):
        """夏日刮一刮：仅领 2508-01（去赚积分）换 1 次次数；无此可领任务则跳过。"""
        if not await self.uphone_activity_login(UPHONE_ACT_SUMMER):
            return 0
        tasks, left = await self.uphone_task_list(UPHONE_ACT_SUMMER)
        pick = next(
            (
                t
                for t in tasks
                if str(t.get("taskCode") or "") == UPHONE_SUMMER_CLAIM_TASK
                and t.get("status") == "UNCLAIMED"
            ),
            None,
        )
        if not pick:
            self.tlog(
                f"夏日 {UPHONE_SUMMER_CLAIM_TASK} 无可领 "
                f"rafflesLeft={left}（已领过或未达条件，跳过）"
            )
            return left

        code = UPHONE_SUMMER_CLAIM_TASK
        name = pick.get("taskName") or code
        ok, body = await self.uphone_raffle_get(UPHONE_ACT_SUMMER, code)
        if ok:
            self.tlog(f"夏日领次数成功 {code} {name}")
        else:
            msg = (body or {}).get("msg") if isinstance(body, dict) else body
            biz = (body or {}).get("code") if isinstance(body, dict) else None
            if biz in (10301, "10301"):
                self.tlog(f"夏日领次数跳过(日限) {code}: {msg}")
            else:
                self.tlog(f"夏日领次数失败 {code}: {msg or body}")
        _, left2 = await self.uphone_task_list(UPHONE_ACT_SUMMER)
        self.tlog(f"夏日抽奖次数 rafflesLeft={left}→{left2}")
        return left2

    async def uphone_summer_lottery(self, max_draws=20):
        draws = 0
        while draws < max_draws:
            _, left = await self.uphone_task_list(UPHONE_ACT_SUMMER)
            if left <= 0:
                break
            r = await self.req(
                "POST",
                f"{UPHONE_H5API}/activity-service/lottery",
                headers=self.uphone_h5_headers(),
                json={"activityCode": UPHONE_ACT_SUMMER},
            )
            body = self.uphone_biz_data(r)
            if not body or not self.uphone_biz_ok(body):
                self.tlog(f"夏日抽奖失败: {api_response_err(r, 'lottery')}")
                break
            draws += 1
            self.tlog(
                f"夏日抽奖#{draws} {body.get('prizeName') or body.get('msg')} "
                f"type={body.get('prizeType')}"
            )
            await asyncio.sleep(0.4)
        if draws == 0:
            self.tlog("夏日无抽奖次数")
        else:
            self.tlog(f"夏日抽奖结束 draws={draws}")
        return draws

    async def uphone_checkin_query(self):
        """查询打卡挑战赛状态。"""
        body = await self.uphone_checkin_post("activity/querySignInList")
        if not body:
            self.tlog("打卡查询无响应")
            return None
        if str(body.get("code")) == "50020":
            self.tlog("打卡 token 失效(50020)")
            return None
        if not self.uphone_biz_ok(body):
            self.tlog(f"打卡查询失败: {body.get('msg') or body}")
            return None
        return body

    async def uphone_checkin_raffle(self, order_id, account="", account_type=""):
        """打卡奖品核销 raffleSignIn（字段 activityOrderid 小写 id）。"""
        if not order_id:
            return False
        body = await self.uphone_checkin_post(
            "activity/raffleSignIn",
            {
                "activityOrderid": order_id,
                "account": account or "",
                "accountType": account_type or "",
            },
        )
        if body and self.uphone_biz_ok(body):
            redir = body.get("redirectUrl") or ""
            self.tlog(
                f"打卡领奖成功 order={order_id}"
                + (f" redirect={redir[:80]}" if redir else "")
            )
            return True
        self.tlog(
            f"打卡领奖失败 order={order_id}: "
            f"{(body or {}).get('msg') if isinstance(body, dict) else body}"
        )
        return False

    async def uphone_checkin_handle_prize(self, body, source="签到"):
        """处理签到/奖品中的 activityOrderId；type=2 需 QQ/微信则跳过。"""
        if not isinstance(body, dict):
            return
        order_id = body.get("activityOrderId") or body.get("activityOrderid")
        if not order_id:
            return
        act_type = body.get("activityType")
        name = body.get("activityName") or body.get("name") or ""
        if act_type in UPHONE_CHECKIN_SKIP_ACTIVITY_TYPES:
            self.tlog(
                f"打卡奖品需手填账号，跳过({source}) type={act_type} "
                f"{name} order={order_id}"
            )
            return
        self.tlog(f"打卡中奖({source}) {name or order_id} type={act_type}")
        await self.uphone_checkin_raffle(order_id)
        await asyncio.sleep(0.3)

    async def uphone_checkin_claim_pending(self):
        """我的奖品：自动领取可核销订单（跳过 type=2）。"""
        body = await self.uphone_checkin_post("activity/signInRightList")
        if not body or not self.uphone_biz_ok(body):
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
            if act_type in UPHONE_CHECKIN_SKIP_ACTIVITY_TYPES:
                self.tlog(f"打卡待领跳过(需账号) {name} type={act_type}")
                continue
            # state=2 历史已领过的不重复核销，仅补 state=1
            if state not in (1, "1"):
                continue
            self.tlog(f"打卡补领 {name} order={order_id}")
            if await self.uphone_checkin_raffle(order_id):
                claimed += 1
            await asyncio.sleep(0.3)
        self.tlog(f"打卡奖品补领结束 claimed={claimed} total={len(goods)}")

    async def uphone_checkin(self):
        """打卡挑战赛 CLD13：query → signIn → 条件 raffle → 奖品补领。

        复用 uphone_sso_login 的 cpToken，无需 activity-service 登录。
        """
        if not self.uphone_cp_token:
            self.tlog("打卡跳过: 无 cpToken")
            return

        info = await self.uphone_checkin_query()
        if info is None:
            return
        day = info.get("signDayNum")
        left = info.get("surpriseDayNum")
        self.tlog(f"打卡状态 已签{day}天 再签{left}天有惊喜")
        if info.get("refreshFlag") == "refresh":
            self.tlog("打卡完成一轮，开启新周期")

        body = await self.uphone_checkin_post("activity/signIn")
        if not body:
            self.tlog("打卡签到无响应")
            return
        code = str(body.get("code", ""))
        msg = body.get("msg") or ""
        if code == "30038":
            self.tlog(f"打卡今日已签: {msg or '今日已签过'}")
        elif self.uphone_biz_ok(body):
            self.tlog(f"打卡成功: {msg or 'ok'}")
            await self.uphone_checkin_handle_prize(body, source="签到")
            await asyncio.sleep(0.3)
            info2 = await self.uphone_checkin_query()
            if info2:
                self.tlog(
                    f"打卡后 已签{info2.get('signDayNum')}天 "
                    f"再签{info2.get('surpriseDayNum')}天有惊喜"
                )
        else:
            self.tlog(f"打卡失败[{code}]: {msg or body}")

        await self.uphone_checkin_claim_pending()

    async def uphone_task(self):
        """云手机：SSO → 打卡挑战赛 → 积分签到/任务/十连 → 夏日刮一刮"""
        self._task_tag = "云手机积分"
        self.tlog("开始")
        if not await self.uphone_sso_login():
            return

        # 打卡挑战赛仅需 cpToken
        await self.uphone_checkin()

        if not await self.uphone_activity_login(UPHONE_ACT_SIGN):
            return

        await self.uphone_sign()
        await self.uphone_obtain_tasks()
        await self.uphone_lottery_10()
        await self.uphone_summer_claim_one()
        await self.uphone_summer_lottery()
        self.tlog("结束")
