"""首页签到。"""

import asyncio
import random
import string

from cu_tasks.base import Task
from cu_tasks.common import api_response_err, require_dict_data

TASK_BROWSE_WAIT = (2.0, 3.0)
TASK_BROWSE_TIMEOUT = 8.0
TASK_STEP_INTERVAL = 1.0
TASK_LIST_MAX_ROUNDS = 6


class SignTask(Task):
    name, label = "sign", "签到区"

    def __init__(self, account, settings=None):
        super().__init__(account, settings)
        self.gettaskip_net_warned = False
        self.initial_amount: float | None = None
        self._initial_tel_data: dict | None = None

    async def get_task_ip(self):
        """上报任务 IP；本地 orderId 始终可用。接口常返回非标准 JSON，失败不刷屏。"""
        order_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=32))
        r = await self.req(
            "POST",
            "https://m.client.10010.com/taskcallback/topstories/gettaskip",
            data={"mobile": self.mobile, "orderId": order_id},
        )
        # 协议/JSON 脏响应极常见且不影响 completeTask，静默；仅真实断网首次提示
        if r.get("code") == -1 and not self.gettaskip_net_warned:
            self.gettaskip_net_warned = True
            self.tlog(
                f"gettaskip网络失败(后续忽略): {api_response_err(r, 'gettaskip')}"
            )
        return order_id

    async def get_continuous(self) -> bool:
        r = await self.req(
            "GET",
            "https://activity.10010.com/sixPalaceGridTurntableLottery/signin/getContinuous",
            params={"taskId": "", "channel": "wode", "imei": self.uuid},
        )
        res = require_dict_data(r, "查询签到状态")
        if str(res.get("code")) != "0000":
            err = api_response_err(r, "查询签到状态")
            self.tlog(f"查询签到状态失败: {err}")
            return False
        signed = res.get("data", {}).get("todayIsSignIn", "n") == "y"
        self.tlog(f"今天{'已' if signed else '未'}签到")
        if not signed:
            await asyncio.sleep(1)
            await self.day_sign()
            return True
        return False

    async def day_sign(self):
        r = await self.req(
            "POST",
            "https://activity.10010.com/sixPalaceGridTurntableLottery/signin/daySign",
            data={},
        )
        res = require_dict_data(r, "每日签到")
        code = str(res.get("code", ""))
        if code == "0000":
            data = res.get("data", {}) or {}
            self.tlog(
                f"签到成功: [{data.get('statusDesc', '')}]{data.get('redSignMessage', '')}"
            )
        elif code == "0002" and "已经签到" in str(res.get("desc", "")):
            self.tlog("签到成功: 今日已完成签到！")
        else:
            self.tlog(f"签到失败[{code}]: {res.get('desc', '')}")

    async def get_telephone(self, is_initial=False, cached_data=None):
        if cached_data is not None:
            tel_data = cached_data
        else:
            r = await self.req(
                "POST", "https://act.10010.com/SigninApp/convert/getTelephone", data={}
            )
            res = require_dict_data(r, "查询话费红包")
            if str(res.get("status")) != "0000" or not isinstance(
                res.get("data"), dict
            ):
                self.tlog(f"查询话费红包失败: {api_response_err(r, '查询话费红包')}")
                return None
            tel_data = res["data"]
        tel_val = tel_data.get("telephone", 0)
        try:
            amount = float(tel_val)
        except (TypeError, ValueError):
            amount = 0.0
        if is_initial:
            self.initial_amount = amount
            self._initial_tel_data = tel_data
            self.tlog(f"话费红包: 运行前总额 {amount:.2f}元")
        else:
            if self.initial_amount is not None:
                self.tlog(
                    f"话费红包: 本次运行增加 {amount - self.initial_amount:.2f}元"
                )
            msg = f"话费红包: 总额 {amount:.2f}元"
            try:
                exp_num = float(tel_data.get("needexpNumber", 0) or 0)
            except (TypeError, ValueError):
                exp_num = 0.0
            if exp_num > 0:
                msg += (
                    f"，其中 {tel_data.get('needexpNumber', '0')}元 "
                    f"将于 {tel_data.get('month', '')}月底到期"
                )
            self.tlog(msg)
        return amount

    async def do_task_from_list(self, task) -> tuple[bool, bool]:
        url = task.get("url") or ""
        if url != "1" and url.startswith("http"):
            await self.req(
                "GET",
                url,
                headers={"Referer": "https://img.client.10010.com/"},
                timeout=TASK_BROWSE_TIMEOUT,
            )
            self.tlog(f"任务中心: 浏览页面 [{task.get('taskName')}]")
            await asyncio.sleep(random.uniform(*TASK_BROWSE_WAIT))
        order_id = await self.get_task_ip()
        r = await self.req(
            "GET",
            "https://activity.10010.com/sixPalaceGridTurntableLottery/task/completeTask",
            params={
                "taskId": task.get("id"),
                "orderId": order_id,
                "systemCode": "QDQD",
            },
        )
        res = require_dict_data(r, "完成签到任务")
        code = str(res.get("code", ""))
        desc = str(res.get("desc", "未知错误"))
        if code == "0329" or "火爆" in desc:
            self.tlog("系统繁忙(0329)，停止后续尝试")
            return False, True
        if code == "0000":
            self.tlog(f"任务中心: 任务 [{task.get('taskName')}] 已完成")
            return True, False
        self.tlog(f"任务中心: 任务 [{task.get('taskName')}] 完成失败[{code}]: {desc}")
        return False, False

    async def get_task_reward(
        self, task_id, task_type=None, record_id=None
    ) -> tuple[bool, bool]:
        params = {"taskId": task_id}
        if task_type is not None:
            params["taskType"] = task_type
        if record_id is not None:
            params["id"] = record_id
        headers = (
            {"Referer": "https://img.client.10010.com/"}
            if task_type is not None
            else None
        )
        r = await self.req(
            "GET",
            "https://activity.10010.com/sixPalaceGridTurntableLottery/task/getTaskReward",
            params=params,
            headers=headers or {},
        )
        res = require_dict_data(r, "领取任务奖励")
        code = str(res.get("code", ""))
        data = res.get("data", {}) or {}
        inner_code = str(data.get("code", ""))
        desc = str(data.get("desc") or res.get("desc", ""))
        if code == "0329" or inner_code == "0329" or "火爆" in desc:
            self.tlog("系统繁忙(0329)，停止后续尝试")
            return False, True
        if code == "0000" and inner_code == "0000":
            prize = (
                f"[{data.get('prizeName', '')}] {data.get('prizeNameRed', '')}".strip()
            )
            self.tlog(f"领取奖励: {prize or data.get('statusDesc', '领取成功')}")
            return True, False
        self.tlog(f"领取奖励失败[{inner_code or code}]: {desc}")
        return False, False

    async def get_task_list(self) -> bool:
        url = "https://activity.10010.com/sixPalaceGridTurntableLottery/task/taskList"
        headers = {"Referer": "https://img.client.10010.com/"}
        attempted_do: set = set()
        attempted_claim: set = set()
        acted = False
        for i in range(TASK_LIST_MAX_ROUNDS):
            r = await self.req("GET", url, params={"type": "2"}, headers=headers)
            res = require_dict_data(r, "查询任务列表")
            code = str(res.get("code", ""))
            if code == "0329" or "火爆" in str(res.get("desc", "")):
                self.tlog("系统繁忙(0329)，停止后续尝试")
                break
            if code != "0000":
                self.tlog(f"任务中心: 获取任务列表失败[{code}]: {res.get('desc', '')}")
                return acted
            tag_list = res.get("data", {}).get("tagList", []) or []
            task_list = res.get("data", {}).get("taskList", []) or []
            all_tasks = task_list + [
                t for tag in tag_list for t in tag.get("taskDTOList", [])
            ]
            all_tasks = [t for t in all_tasks if isinstance(t, dict)]
            if not all_tasks:
                if i == 0:
                    self.tlog("任务中心: 当前无任何任务。")
                break
            do_tasks = [
                t
                for t in all_tasks
                if t.get("taskState") == "1"
                and t.get("taskType") == "5"
                and t.get("id") not in attempted_do
            ]
            if do_tasks:
                for idx, do_task in enumerate(do_tasks):
                    attempted_do.add(do_task.get("id"))
                    acted = True
                    self.tlog(f"任务中心: 开始执行任务 [{do_task.get('taskName')}]")
                    _, busy = await self.do_task_from_list(do_task)
                    if busy:
                        return acted
                    if idx + 1 < len(do_tasks):
                        await asyncio.sleep(TASK_STEP_INTERVAL)
                await asyncio.sleep(TASK_STEP_INTERVAL)
                continue
            claim_tasks = [
                t
                for t in all_tasks
                if t.get("taskState") == "0" and t.get("id") not in attempted_claim
            ]
            if claim_tasks:
                for idx, claim_task in enumerate(claim_tasks):
                    attempted_claim.add(claim_task.get("id"))
                    acted = True
                    self.tlog(
                        f"任务中心: 发现可领取奖励的任务 [{claim_task.get('taskName')}]"
                    )
                    _, busy = await self.get_task_reward(claim_task.get("id"))
                    if busy:
                        return acted
                    if idx + 1 < len(claim_tasks):
                        await asyncio.sleep(TASK_STEP_INTERVAL)
                # 本轮 do_tasks 已为空，领完存量奖励后无需再发第 3 次 taskList 查询
                self.tlog("任务中心: 所有任务处理完毕。")
                break
            if not acted:
                self.tlog("任务中心: 没有可执行或可领取的任务。")
            else:
                self.tlog("任务中心: 所有任务处理完毕。")
            break
        return acted

    async def get_month_sign_reward(self, task):
        task_name = task.get("taskName") or "月签奖励"
        r = await self.req(
            "GET",
            "https://activity.10010.com/sixPalaceGridTurntableLottery/task/getTaskReward",
            params={
                "taskId": task.get("taskId"),
                "taskType": "30",
                "id": task.get("id"),
            },
            headers={"Referer": "https://img.client.10010.com/"},
        )
        res = require_dict_data(r, "领取月签奖励")
        code = str(res.get("code", ""))
        data = res.get("data", {}) or {}
        if code == "0000" and str(data.get("code", "")) == "0000":
            prize_name = data.get("prizeName", "")
            prize_red = data.get("prizeNameRed", "")
            reward = (
                f"[{prize_name}] {prize_red}".strip()
                if prize_name or prize_red
                else data.get("statusDesc", "领取成功")
            )
            self.tlog(f"月签有礼领取成功: [{task_name}] {reward}")
        else:
            msg = data.get("desc") or res.get("desc") or res.get("msg") or "未知错误"
            self.tlog(
                f"月签有礼: [{task_name}] 领取失败[{data.get('code') or code}]: {msg}"
            )

    async def month_sign_gift(self) -> int:
        r = await self.req(
            "GET",
            "https://activity.10010.com/sixPalaceGridTurntableLottery/floor/getMonthSign",
            headers={"Referer": "https://img.client.10010.com/"},
        )
        res = require_dict_data(r, "查询月签有礼")
        if str(res.get("code")) != "0000":
            self.tlog(f"月签有礼: 查询失败[{res.get('code')}]: {res.get('desc', '')}")
            return 0
        task_list = res.get("data", {}).get("taskList", []) or []
        if not task_list:
            self.tlog("月签有礼: 暂无月签任务")
            return 0
        claim_tasks = [
            t
            for t in task_list
            if str(t.get("taskStatus")) == "1" and t.get("taskId") and t.get("id")
        ]
        claimed_count = sum(1 for t in task_list if str(t.get("taskStatus")) == "2")
        if not claim_tasks:
            self.tlog(
                f"月签有礼: 暂无可领取奖励，已领取 {claimed_count}/{len(task_list)}"
            )
            return 0
        for task in claim_tasks:
            await self.get_month_sign_reward(task)
            await asyncio.sleep(1)
        return len(claim_tasks)

    async def query_my_prizes(self):
        r = await self.req(
            "POST",
            "https://act.10010.com/SigninApp/convert/phoneDetails",
            data={"log_type": "1", "number": "1", "list_num": ""},
            headers={"Origin": "https://img.client.10010.com"},
        )
        res = require_dict_data(r, "查询账户明细")
        if str(res.get("status")) != "0000":
            return
        data = res.get("data", {}).get("detailedBO", []) or []
        logged = 0
        for item in data:
            if logged >= 5:
                break
            remark = item.get("remark", "")
            buss_name = item.get("from_bussname", "")
            if "兑换" not in remark and "兑换" not in buss_name:
                continue
            if logged == 0:
                self.tlog("账户明细: 最近兑换记录")
            self.tlog(
                f"  {item.get('order_time', '')} | {remark} (变动:{item.get('booksNumber') or item.get('books_number') or '0'})"
            )
            logged += 1

    async def run(self):
        await self.get_telephone(is_initial=True)
        signed = await self.get_continuous()
        month_claimed = await self.month_sign_gift()
        task_acted = await self.get_task_list()
        if signed or month_claimed or task_acted or self._initial_tel_data is None:
            await self.get_telephone()
        else:
            await self.get_telephone(cached_data=self._initial_tel_data)
        await self.query_my_prizes()
