"""福利商城 ninePalaceGrid 抽奖基类与九宫格任务，协议依据 2026-10-31 / 2026-11-16 HAR。"""

import asyncio
import json
import uuid

from cu_tasks.base import Task
from cu_tasks.common import api_response_err, require_int

BASE_URL = "https://activity.10010.com/welfare-mall-front/ninePalaceGrid"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "unicom{version:iphone_c@13.0100};ltst;OSVersion/27.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://img.client.10010.com",
    "Referer": "https://img.client.10010.com/",
}
RESULT_WAIT_SECONDS = 3
RESULT_POLL_SECONDS = 1
RESULT_MAX_QUERIES = 4
# HAR 实测服务端同用户连续抽奖冷却约 10.5s；叠加上轮查奖耗时（3.3~6.5s）共约 15.5~18.5s
DRAW_INTERVAL_SECONDS = 12


class NinePalaceGridTask(Task):
    """ninePalaceGrid 公共流程：仅消费查询到的次数；写请求不重试，结果不确定时停止。"""

    def grid_config(self) -> tuple[str, str, int, int | None]:
        """返回 (activity_id, source, max_draws, max_cost)；max_cost 为 None 不限制。"""
        raise NotImplementedError

    async def api(self, action: str, payload: dict) -> dict:
        response = await self.req(
            "POST",
            f"{BASE_URL}/{action}/v1",
            headers=HEADERS,
            data={
                "params": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            },
        )
        body = response.get("data")
        if (
            response.get("code") != 200
            or response.get("error")
            or not isinstance(body, dict)
        ):
            raise ValueError(api_response_err(response, action))
        # 已确认的业务结果仅在对应接口放行，不能当作通用成功码。
        accepted = {"0"}
        if action == "findWinInfo":
            accepted.add("1123")  # 未中奖
        elif action == "luckyDraw":
            accepted.update({"1123", "1124", "1126", "1129"})
        if isinstance(body.get("code"), bool) or str(body.get("code")) not in accepted:
            raise ValueError(f"{action}: code={body.get('code')} {body.get('msg', '')}")
        return body

    @staticmethod
    def resdata(body: dict, action: str) -> dict:
        data = body.get("resdata")
        if not isinstance(data, dict):
            raise ValueError(f"{action}: resdata 应为对象")
        return data

    async def draw_count(self, payload: dict) -> int:
        body = await self.api("findLotteryCount", payload)
        data = self.resdata(body, "findLotteryCount")
        return require_int(data.get("drawCount"), "drawCount", minimum=0)

    def check_cost(self, activity: dict, max_cost: int | None) -> bool:
        if max_cost is None:
            return True
        luck_type = str(activity.get("luckType", ""))
        raw_cost = activity.get("consumptionIntegral")
        if raw_cost in (None, ""):
            if luck_type == "1":
                self.tlog("活动消耗积分配置缺失(luckType=1)，跳过抽奖")
                return False
            cost = 0
        else:
            cost = require_int(raw_cost, "consumptionIntegral", minimum=0)
        if cost > max_cost:
            self.tlog(f"单次抽奖消耗 {cost} 积分，超过上限 {max_cost}，跳过")
            return False
        return True

    async def query_result(self, payload: dict, *, log_exhausted: bool = True) -> bool:
        # 新 HAR 中 false 可能是尚未出结果或未中奖；同一 reqSeq 最多查 4 次。
        for query in range(RESULT_MAX_QUERIES):
            if query:
                await asyncio.sleep(RESULT_POLL_SECONDS)
            body = await self.api("findWinInfo", payload)
            if str(body.get("code")) == "1123":
                self.tlog("抽奖结果: 未中奖")
                return True
            data = self.resdata(body, "findWinInfo")
            won = data.get("isWin")
            if won is True or won == "true":
                name = data.get("prizeName")
                self.tlog(f"抽奖中奖: {name or '奖品名称未返回，请到活动页查看'}")
                return True
            if won is not False and won != "false":
                raise ValueError("findWinInfo: 缺少有效 isWin，停止后续抽奖")
        if log_exhausted:
            self.tlog(f"抽奖结果: 连续 {RESULT_MAX_QUERIES} 次查询暂无中奖信息")
        return False

    async def run(self) -> None:
        activity_id, source, max_draws, max_cost = self.grid_config()
        if max_draws == 0:
            self.tlog("抽奖上限为 0，跳过")
            return
        payload = {"from": source, "acId": activity_id}
        activity = self.resdata(await self.api("findActivity", payload), "findActivity")
        # 仅接受抓包已验证的进行中状态和目标用户标记。
        if str(activity.get("acState")) != "2":
            self.tlog(f"活动未开放或状态未知: {activity.get('acState')}")
            return
        if activity.get("isTarget") is not True and activity.get("isTarget") != "true":
            self.tlog("当前账号不满足活动参与条件")
            return
        if not self.check_cost(activity, max_cost):
            return
        count = await self.draw_count(payload)
        budget = min(count, max_draws)
        self.tlog(
            f"剩余 {count} 次，本轮最多抽奖 {budget} 次；"
            f"活动返回消耗积分配置 {activity.get('consumptionIntegral', '未知')}"
        )
        for attempt in range(budget):
            if attempt:
                await asyncio.sleep(DRAW_INTERVAL_SECONDS)
            # 每次抽奖生成新 reqSeq；查询结果必须复用，不携带抓包中的设备 ID。
            draw = {**payload, "reqSeq": uuid.uuid4().hex, "imei": self.uuid.upper()}
            try:
                submitted = await self.api("luckyDraw", draw)
            except ValueError as exc:
                self.tlog_exc(exc, "抽奖提交失败或结果不确定，停止继续提交")
                # 请求可能已落地；同一 reqSeq 有限查询结果，不重发抽奖。
                await asyncio.sleep(RESULT_WAIT_SECONDS)
                await self.query_result(draw)
                return
            code = str(submitted.get("code"))
            if code == "1124":
                self.tlog("当前账号不满足活动参与条件，停止抽奖")
                return
            if code == "1126":
                self.tlog("抽奖停止: 积分不足")
                return
            busy = code == "1129"
            resolved = True
            if code == "1123":
                self.tlog("抽奖结果: 未中奖")
            else:
                if busy:
                    self.tlog("抽奖返回 1129（活动繁忙），查询结果和剩余次数确认")
                await asyncio.sleep(RESULT_WAIT_SECONDS)
                resolved = await self.query_result(draw, log_exhausted=False)
            remaining = await self.draw_count(draw)
            if not resolved:
                if remaining < count:
                    self.tlog("抽奖结果: 未中奖")
                else:
                    self.tlog(f"抽奖结果: 连续 {RESULT_MAX_QUERIES} 次查询暂无中奖信息")
            if remaining >= count:
                self.tlog("次数未减少，停止继续抽奖，本轮不重发该请求")
                return
            if busy:
                self.tlog(f"繁忙响应后次数已减少: {count} → {remaining}，计入本轮上限")
            if remaining == 0:
                self.tlog("抽奖次数已用完")
                return
            count = remaining


class WelfareTask(NinePalaceGridTask):
    """福利商城九宫格。"""

    name, label = "welfare", "福利商城九宫格"

    def grid_config(self) -> tuple[str, str, int, int | None]:
        return (
            self.settings.welfare_activity_id,
            self.settings.welfare_source,
            self.settings.welfare_max_draws,
            None,
        )
