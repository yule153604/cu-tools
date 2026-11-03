"""安全管家：ecs_token → uca 授权 → 积分 ticket → 积分中心签到并领取。"""

import random
import string
from datetime import datetime
from urllib.parse import unquote

from cu_tasks.base import Task
from cu_tasks.common import api_response_err, require_dict_data
from cu_tasks.jftask import JfTaskClient, task_pending

EDOP_APP_ID = "edop_unicom_3a6cc75a"
UCA = "https://uca.wo116114.com/api/v1/auth"
UCA_QUERY = f"product_line=uasp&entry_point=h5&entry_point_id={EDOP_APP_ID}"
CLIENT_TYPE = "uasp_unicom_applet"
PRODUCT_ID = "91311616"
PAGE_ID = "s768590754920323072"
APP_UA = (
    "ChinaUnicom4.x/12.3.1 (com.chinaunicom.mobilebusiness; build:77; iOS 16.6.0) "
    "Alamofire/4.7.3"
)
SEC_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15"


class SecurityTask(Task):
    name, label = "sec", "安全管家"

    async def uca_auth(self, ticket):
        r = await self.req(
            "POST",
            f"{UCA}/ticket?{UCA_QUERY}",
            headers={"clientType": CLIENT_TYPE, "Content-Type": "application/json"},
            json={"productId": "", "type": 1, "ticket": ticket},
        )
        data = require_dict_data(r, "安全管家auth/ticket").get("data")
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            self.tlog(f"授权失败: {api_response_err(r, 'auth/ticket')}")
        return token or ""

    async def points_ticket(self, sa_token):
        r = await self.req(
            "POST",
            f"{UCA}/getTicket?{UCA_QUERY}",
            headers={
                "auth-sa-token": sa_token,
                "clientType": CLIENT_TYPE,
                "Content-Type": "application/json",
            },
            json={"productId": PRODUCT_ID, "phone": self.mobile},
        )
        data = require_dict_data(r, "安全管家getTicket").get("data")
        ticket = data.get("ticket") if isinstance(data, dict) else None
        if not ticket:
            self.tlog(f"积分 ticket 获取失败: {api_response_err(r, 'getTicket')}")
        return unquote(ticket) if ticket else ""

    async def run(self):
        if not self.ecs_token or not self.mobile:
            self.tlog("缺少登录信息，跳过")
            return
        session_id = datetime.now().strftime("%Y%m%d%H%M%S") + "".join(
            random.choices(string.ascii_letters + string.digits, k=32)
        )
        ticket = await self.edop_ticket(
            EDOP_APP_ID,
            headers={
                "Cookie": (
                    f"PvSessionId={session_id};c_mobile={self.mobile};"
                    f"c_version=iphone_c@11.0800;ecs_token={self.ecs_token}"
                ),
                "User-Agent": APP_UA,
            },
        )
        if not ticket:
            return
        sa_token = await self.uca_auth(ticket)
        if not sa_token:
            return
        points_ticket = await self.points_ticket(sa_token)
        if not points_ticket:
            return

        jf = JfTaskClient(
            self,
            ticket=points_ticket,
            partners_id="1702",
            client_type=CLIENT_TYPE,
            user_agent=SEC_UA,
        )
        if not await jf.init(extra_headers={"pageId": PAGE_ID}):
            return
        sign = next(
            (
                t
                for t in await jf.task_list()
                if "签到" in t.get("taskName", "") and task_pending(t)
            ),
            None,
        )
        if not sign:
            self.tlog("今日签到已完成或无签到任务")
            return
        code = sign.get("taskCode")
        # 与原流程一致：签到后总是尝试领取，两步结果分别记录
        for path, action in (("uasptask/sign", "签到"), ("jftask/receive", "领取奖励")):
            r = await jf.post(path, {"taskCode": code})
            res = r.get("data")
            if isinstance(res, dict) and str(res.get("code")) == "0000":
                self.tlog(f"{action}成功")
            else:
                self.tlog(f"{action}失败: {api_response_err(r, path)}")
