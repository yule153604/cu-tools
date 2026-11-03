"""积分任务中心 m.jf.10010.com：getSecretKey 取密钥后按 HMAC-SHA256 签名请求。

安全管家（partnersid=1702）与沃阅读/爱听（partnersid=1706）共用此协议。
"""

import hashlib
import hmac
import random
import string
import time

from cu_tasks.common import api_response_err, find_set_cookie, require_dict_data

JF_BASE = "https://m.jf.10010.com/jf-external-application"


class JfTaskClient:
    def __init__(
        self,
        task,
        *,
        ticket,
        partners_id,
        client_type,
        user_agent,
        extra=None,
        signed_extra=None,
    ):
        self.task = task
        self.ticket = ticket
        self.base_headers = {
            "ticket": ticket,
            "partnersid": partners_id,
            "clienttype": client_type,
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            **(extra or {}),
        }
        self.signed_extra = signed_extra or {}
        self.secret_key = ""
        self.jea_id = ""

    async def init(self, extra_headers=None) -> bool:
        """获取签名密钥与 _jea_id；失败写日志并返回 False。"""
        r = await self.task.req(
            "GET",
            f"{JF_BASE}/jftask/getSecretKey",
            headers={**self.base_headers, **(extra_headers or {})},
        )
        self.jea_id = find_set_cookie(r.get("headers"), "_jea_id")
        res = r.get("data")
        if not isinstance(res, dict) or str(res.get("code")) != "0000":
            self.task.tlog(f"getSecretKey 失败: {api_response_err(r, 'getSecretKey')}")
            return False
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        self.secret_key = str(data.get("secretKey") or "")
        if not self.secret_key:
            self.task.tlog("getSecretKey 未返回密钥")
            return False
        return True

    def signed_headers(self):
        ts = str(int(time.time() * 1000))
        nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        sig = hmac.new(
            self.secret_key.encode(), f"{nonce}{ts}".encode(), hashlib.sha256
        ).hexdigest()
        return {
            **self.base_headers,
            **self.signed_extra,
            "Cookie": f"_jea_id={self.jea_id}",
            "Content-Type": "application/json;charset=UTF-8",
            "X-Request-Timestamp": ts,
            "X-Request-Nonce": nonce,
            "X-Request-Signature": sig,
        }

    async def post(self, path, payload=None):
        """签名 POST；返回 req() 统一响应。"""
        return await self.task.req(
            "POST",
            f"{JF_BASE}/{path.lstrip('/')}",
            headers=self.signed_headers(),
            json=payload or {},
        )

    async def task_list(self):
        r = await self.post("jftask/taskDetail")
        res = require_dict_data(r, "taskDetail")
        if str(res.get("code")) != "0000":
            raise ValueError(api_response_err(r, "taskDetail"))
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        detail = (
            data.get("taskDetail") if isinstance(data.get("taskDetail"), dict) else {}
        )
        tasks = detail.get("taskList")
        return (
            [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []
        )

    async def user_info(self):
        r = await self.post("jftask/userInfo")
        res = r.get("data")
        if isinstance(res, dict) and str(res.get("code")) == "0000":
            return res.get("data") if isinstance(res.get("data"), dict) else {}
        return None


def task_pending(task) -> bool:
    """finishCount < needCount 视为未完成（缺省 needCount=1）。"""
    try:
        return int(task.get("finishCount") or 0) < int(task.get("needCount") or 1)
    except (TypeError, ValueError):
        return False
