"""福利商城积分九宫格，协议依据 2026-10-31 HAR；使用当前账号 Cookie。"""

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
DRAW_INTERVAL_SECONDS = 30


class WelfareTask(Task):
    """仅消费查询到的次数；写请求不重试，结果不确定时停止。"""

    name, label = "welfare", "福利商城九宫格"

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
        # 两个已确认的业务结果仅在对应接口放行，不能当作通用成功码。
        accepted = {"0"}
        if action == "findWinInfo":
            accepted.add("1123")  # 未中奖
        elif action == "luckyDraw":
            accepted.add("1129")  # 需查询次数确认是否消耗
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

    async def query_result(self, payload: dict) -> None:
        # 新 HAR 中 false 可能是尚未出结果；同一 reqSeq 最多查 4 次。
        for query in range(RESULT_MAX_QUERIES):
            if query:
                await asyncio.sleep(RESULT_POLL_SECONDS)
            body = await self.api("findWinInfo", payload)
            if str(body.get("code")) == "1123":
                self.tlog("抽奖结果: 未中奖")
                return
            data = self.resdata(body, "findWinInfo")
            won = data.get("isWin")
            if won is True or won == "true":
                name = data.get("prizeName")
                self.tlog(f"抽奖中奖: {name or '奖品名称未返回，请到活动页查看'}")
                return
            if won is not False and won != "false":
                raise ValueError("findWinInfo: 缺少有效 isWin，停止后续抽奖")
        self.tlog(f"抽奖结果: 连续 {RESULT_MAX_QUERIES} 次查询暂无中奖信息")

    async def run(self) -> None:
        max_draws = self.settings.welfare_max_draws
        if max_draws == 0:
            self.tlog("抽奖上限为 0，跳过")
            return
        payload = {
            "from": self.settings.welfare_source,
            "acId": self.settings.welfare_activity_id,
        }
        activity = self.resdata(await self.api("findActivity", payload), "findActivity")
        # 仅接受抓包已验证的进行中状态和目标用户标记。
        if str(activity.get("acState")) != "2":
            self.tlog(f"活动未开放或状态未知: {activity.get('acState')}")
            return
        if activity.get("isTarget") is not True and activity.get("isTarget") != "true":
            self.tlog("当前账号不满足活动参与条件")
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
            busy = str(submitted.get("code")) == "1129"
            if busy:
                self.tlog("抽奖返回 1129（活动繁忙），查询结果和剩余次数确认")
            await asyncio.sleep(RESULT_WAIT_SECONDS)
            await self.query_result(draw)
            remaining = await self.draw_count(draw)
            if remaining >= count:
                self.tlog("次数未减少，停止继续抽奖，本轮不重发该请求")
                return
            if busy:
                self.tlog(f"繁忙响应后次数已减少: {count} → {remaining}，计入本轮上限")
            if remaining == 0:
                self.tlog("抽奖次数已用完")
                return
            count = remaining
