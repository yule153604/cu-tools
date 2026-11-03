"""安全管家。"""

import hashlib
import hmac
import random
import string
import time
from datetime import datetime
from urllib.parse import unquote

from cu_tasks.common import (
    SEC_UA,
    iter_set_cookies,
    require_dict_data,
)


class SecurityTasks:
    """活动实现，复用宿主的账号会话、请求与日志接口。"""

    async def sec_task(self):
        self._task_tag = "安全管家"
        self.tlog("开始")
        if not self.ecs_token or not self.mobile:
            return

        # 获取ticket
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        r = await self.req(
            "GET",
            f"https://m.client.10010.com/edop_ng/getTicketByNative?token={self.ecs_token}&appId=edop_unicom_3a6cc75a",
            headers={
                "Cookie": f"PvSessionId={ts}{''.join(random.choices(string.ascii_letters + string.digits, k=32))};c_mobile={self.mobile};c_version=iphone_c@11.0800;ecs_token={self.ecs_token}",
                "User-Agent": "ChinaUnicom4.x/12.3.1 (com.chinaunicom.mobilebusiness; build:77; iOS 16.6.0) Alamofire/4.7.3",
            },
        )
        ticket1 = require_dict_data(r, "安全管家ticket").get("ticket")
        if not ticket1:
            return

        # 获取token
        r = await self.req(
            "POST",
            "https://uca.wo116114.com/api/v1/auth/ticket?product_line=uasp&entry_point=h5&entry_point_id=edop_unicom_3a6cc75a",
            headers={
                "clientType": "uasp_unicom_applet",
                "Content-Type": "application/json",
            },
            json={"productId": "", "type": 1, "ticket": ticket1},
        )
        res = require_dict_data(r, "安全管家auth/ticket")
        if not res.get("data"):
            return
        self.sec_token = res["data"].get("access_token")
        if not self.sec_token:
            return

        # 获取积分ticket
        r = await self.req(
            "POST",
            "https://uca.wo116114.com/api/v1/auth/getTicket?product_line=uasp&entry_point=h5&entry_point_id=edop_unicom_3a6cc75a",
            headers={
                "auth-sa-token": self.sec_token,
                "clientType": "uasp_unicom_applet",
                "Content-Type": "application/json",
            },
            json={"productId": "91311616", "phone": self.mobile},
        )
        res = require_dict_data(r, "安全管家getTicket")
        if not res.get("data"):
            return
        self.sec_ticket = res["data"].get("ticket")

        # 获取密钥
        sec_jea_id = ""
        t = unquote(self.sec_ticket) if self.sec_ticket else ""
        r = await self.req(
            "GET",
            "https://m.jf.10010.com/jf-external-application/jftask/getSecretKey",
            headers={
                "ticket": t,
                "partnersid": "1702",
                "clienttype": "uasp_unicom_applet",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15",
                "pageId": "s768590754920323072",
            },
        )
        for cookie in iter_set_cookies(r["headers"]):
            if "_jea_id=" in cookie:
                sec_jea_id = cookie.split("_jea_id=")[1].split(";")[0]
                break
        res = require_dict_data(r, "获取密钥")
        if str(res.get("code")) != "0000":
            self.tlog(f"获取密钥失败: {res}")
            return
        self.sec_key = res.get("data", {}).get("secretKey")
        if not self.sec_key:
            return
        self.sec_jea_id = sec_jea_id

        # 执行任务
        def sec_headers():
            ts, nonce = (
                str(int(time.time() * 1000)),
                "".join(random.choices(string.ascii_lowercase + string.digits, k=8)),
            )
            sig = hmac.new(
                self.sec_key.encode(), f"{nonce}{ts}".encode(), hashlib.sha256
            ).hexdigest()
            return {
                "ticket": t,
                "User-Agent": SEC_UA,
                "partnersid": "1702",
                "clienttype": "uasp_unicom_applet",
                "Cookie": f"_jea_id={self.sec_jea_id}",
                "X-Request-Timestamp": ts,
                "X-Request-Nonce": nonce,
                "X-Request-Signature": sig,
            }

        # 签到任务
        r = await self.req(
            "POST",
            "https://m.jf.10010.com/jf-external-application/jftask/taskDetail",
            headers=sec_headers(),
            json={},
        )
        if (res := r["data"]) and res.get("data", {}).get("taskDetail"):
            for task in res["data"]["taskDetail"].get("taskList", []):
                if "签到" in task.get("taskName", "") and task.get(
                    "needCount", 1
                ) > task.get("finishCount", 0):
                    await self.req(
                        "POST",
                        "https://m.jf.10010.com/jf-external-application/uasptask/sign",
                        headers=sec_headers(),
                        json={"taskCode": task.get("taskCode")},
                    )
                    await self.req(
                        "POST",
                        "https://m.jf.10010.com/jf-external-application/jftask/receive",
                        headers=sec_headers(),
                        json={"taskCode": task.get("taskCode")},
                    )
                    self.tlog("签到完成")
                    break
