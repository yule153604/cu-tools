"""首页签到。"""

import asyncio
import random
import string

from cu_tasks.common import (
    api_response_err,
    require_dict_data,
)


class SignTasks:
    """活动实现，复用宿主的账号会话、请求与日志接口。"""

    async def sign_get_task_ip(self):
        """上报任务 IP；本地 orderId 始终可用。接口常返回非标准 JSON，失败不刷屏。"""
        order_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=32))
        r = await self.req(
            "POST",
            "https://m.client.10010.com/taskcallback/topstories/gettaskip",
            data={"mobile": self.mobile, "orderId": order_id},
        )
        # 协议/JSON 脏响应极常见且不影响 completeTask，静默；仅真实断网首次提示
        if r.get("code") == -1 and not getattr(self, "_gettaskip_net_warned", False):
            self._gettaskip_net_warned = True
            self.tlog(
                f"gettaskip网络失败(后续忽略): {api_response_err(r, 'gettaskip')}"
            )
        return order_id

    async def sign_get_continuous(self):
        r = await self.req(
            "GET",
            "https://activity.10010.com/sixPalaceGridTurntableLottery/signin/getContinuous",
            params={"taskId": "", "channel": "wode", "imei": self.uuid},
        )
        res = require_dict_data(r, "查询签到状态")
        if str(res.get("code")) != "0000":
            err = api_response_err(r, "查询签到状态")
            self.tlog(f"查询签到状态失败: {err}")
            return
        signed = res.get("data", {}).get("todayIsSignIn", "n") == "y"
        self.tlog(f"今天{'已' if signed else '未'}签到")
        if not signed:
            await asyncio.sleep(1)
            await self.sign_day_sign()

    async def sign_day_sign(self):
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

    async def sign_get_telephone(self, is_initial=False):
        r = await self.req(
            "POST", "https://act.10010.com/SigninApp/convert/getTelephone", data={}
        )
        res = require_dict_data(r, "查询话费红包")
        if str(res.get("status")) != "0000" or not res.get("data"):
            self.tlog(f"查询话费红包失败: {api_response_err(r, '查询话费红包')}")
            return None
        tel_val = res["data"].get("telephone", 0)
        try:
            amount = float(tel_val)
        except (TypeError, ValueError):
            amount = 0.0
        if is_initial:
            self.sign_initial_amount = amount
            self.tlog(f"话费红包: 运行前总额 {amount:.2f}元")
        else:
            if hasattr(self, "sign_initial_amount"):
                self.tlog(
                    f"话费红包: 本次运行增加 {amount - self.sign_initial_amount:.2f}元"
                )
            msg = f"话费红包: 总额 {amount:.2f}元"
            try:
                exp_num = float(res["data"].get("needexpNumber", 0) or 0)
            except (TypeError, ValueError):
                exp_num = 0.0
            if exp_num > 0:
                msg += f"，其中 {res['data'].get('needexpNumber', '0')}元 将于 {res['data'].get('month', '')}月底到期"
            self.tlog(msg)
        return amount

    async def sign_do_task_from_list(self, task):
        url = task.get("url") or ""
        if url != "1" and url.startswith("http"):
            await self.req(
                "GET", url, headers={"Referer": "https://img.client.10010.com/"}
            )
            self.tlog(f"任务中心: 浏览页面 [{task.get('taskName')}]")
            await asyncio.sleep(random.uniform(5, 7))
        order_id = await self.sign_get_task_ip()
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
        if code == "0000":
            self.tlog(f"任务中心: 任务 [{task.get('taskName')}] 已完成")
        else:
            self.tlog(
                f"任务中心: 任务 [{task.get('taskName')}] 完成失败[{code}]: {res.get('desc', '未知错误')}"
            )

    async def sign_get_task_reward(self, task_id, task_type=None, record_id=None):
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
        if code == "0000" and str(data.get("code", "")) == "0000":
            prize = (
                f"[{data.get('prizeName', '')}] {data.get('prizeNameRed', '')}".strip()
            )
            self.tlog(f"领取奖励: {prize or data.get('statusDesc', '领取成功')}")
        else:
            self.tlog(
                f"领取奖励失败[{data.get('code') or code}]: {data.get('desc') or res.get('desc', '')}"
            )

    async def sign_get_task_list(self):
        url = "https://activity.10010.com/sixPalaceGridTurntableLottery/task/taskList"
        headers = {"Referer": "https://img.client.10010.com/"}
        for i in range(30):
            r = await self.req("GET", url, params={"type": "2"}, headers=headers)
            res = require_dict_data(r, "查询任务列表")
            code = str(res.get("code", ""))
            if code == "0329" or "火爆" in str(res.get("desc", "")):
                self.tlog("系统繁忙(0329)，停止后续尝试")
                break
            if code != "0000":
                self.tlog(f"任务中心: 获取任务列表失败[{code}]: {res.get('desc', '')}")
                return
            tag_list = res.get("data", {}).get("tagList", []) or []
            task_list = res.get("data", {}).get("taskList", []) or []
            all_tasks = task_list + [
                t for tag in tag_list for t in tag.get("taskDTOList", [])
            ]
            all_tasks = [t for t in all_tasks if t]
            if not all_tasks:
                if i == 0:
                    self.tlog("任务中心: 当前无任何任务。")
                break
            do_task = next(
                (
                    t
                    for t in all_tasks
                    if t.get("taskState") == "1" and t.get("taskType") == "5"
                ),
                None,
            )
            if do_task:
                self.tlog(f"任务中心: 开始执行任务 [{do_task.get('taskName')}]")
                await self.sign_do_task_from_list(do_task)
                await asyncio.sleep(3)
                continue
            claim_task = next((t for t in all_tasks if t.get("taskState") == "0"), None)
            if claim_task:
                self.tlog(
                    f"任务中心: 发现可领取奖励的任务 [{claim_task.get('taskName')}]"
                )
                await self.sign_get_task_reward(claim_task.get("id"))
                await asyncio.sleep(2)
                continue
            if i == 0:
                self.tlog("任务中心: 没有可执行或可领取的任务。")
            else:
                self.tlog("任务中心: 所有任务处理完毕。")
            break

    async def sign_get_month_sign_reward(self, task):
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
            self.tlog(f"月签有礼: [{task_name}] {reward}")
        else:
            msg = data.get("desc") or res.get("desc") or res.get("msg") or "未知错误"
            self.tlog(
                f"月签有礼: [{task_name}] 领取失败[{data.get('code') or code}]: {msg}"
            )

    async def sign_month_sign_gift(self):
        r = await self.req(
            "GET",
            "https://activity.10010.com/sixPalaceGridTurntableLottery/floor/getMonthSign",
            headers={"Referer": "https://img.client.10010.com/"},
        )
        res = require_dict_data(r, "查询月签有礼")
        if str(res.get("code")) != "0000":
            self.tlog(f"月签有礼: 查询失败[{res.get('code')}]: {res.get('desc', '')}")
            return
        task_list = res.get("data", {}).get("taskList", []) or []
        if not task_list:
            self.tlog("月签有礼: 暂无月签任务")
            return
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
            return
        for task in claim_tasks:
            await self.sign_get_month_sign_reward(task)
            await asyncio.sleep(1)

    async def sign_query_my_prizes(self):
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

    async def sign_task(self):
        self._task_tag = "签到区"
        self.tlog("开始")
        await self.sign_get_telephone(is_initial=True)
        await self.sign_get_continuous()
        await self.sign_month_sign_gift()
        await self.sign_get_task_list()
        await self.sign_get_telephone()
        await self.sign_query_my_prizes()
