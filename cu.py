"""联通脚本入口：账号会话、任务调度与通知。运行方式：python cu.py。"""

import asyncio
import json
import os
import random
import string
import time
import uuid
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx

from cu_tasks.battle import BattleTasks
from cu_tasks.campus import CampusTasks
from cu_tasks.cloud import CloudTasks
from cu_tasks.common import (
    APP_ID,
    APP_VERSION,
    NOTIFY_LINES,
    UA,
    api_response_err,
    format_exception_detail,
    notify_send,
    redact_text,
)
from cu_tasks.farm import FarmTasks
from cu_tasks.market import MarketTasks
from cu_tasks.registry import select_tasks
from cu_tasks.security import SecurityTasks
from cu_tasks.shangdu import ShangduTasks
from cu_tasks.sign import SignTasks
from cu_tasks.uphone import UphoneTasks
from cu_tasks.welfare import WelfareTasks
from cu_tasks.woread import WoreadTasks
from cu_tasks.xj import XinjiangTasks


class Unicom(
    SignTasks,
    MarketTasks,
    XinjiangTasks,
    SecurityTasks,
    FarmTasks,
    ShangduTasks,
    CloudTasks,
    UphoneTasks,
    WoreadTasks,
    CampusTasks,
    BattleTasks,
    WelfareTasks,
):
    def __init__(self, cookie: str, idx: int = 1):
        if not cookie.strip():
            raise ValueError("账号 token_online 不能为空")
        self.cookie, self.idx = cookie.strip(), idx
        self._task_tag = None
        self.cloud_disk = {}
        self.mobile = self.province = self.province_code = self.ecs_token = ""
        self.market_token = self.market_login_id = self.market_cycle_start_time = ""
        self.xj_token = ""
        self.sec_ticket = self.sec_token = self.sec_jea_id = self.sec_key = ""
        self.ttxc_token = self.ttxc_nick_name = self.ttxc_user_id = ""
        self.ttxc_newbie_list = []
        self.ttxc_charge_level = {}
        self.ttxc_no_energy = False
        self.battle_page_referer = ""
        self.last_ticket_url = ""
        self.uphone_cp_token = self.uphone_usr_token = self.uphone_device_id = ""

        # 生成UUID用于签到
        self.uuid = str(uuid.uuid4())

        self.client = httpx.AsyncClient(
            http2=False,
            follow_redirects=False,
            timeout=50.0,
            headers={"User-Agent": UA, "Connection": "keep-alive"},
        )

        # 设置cookie
        self.unicom_token_id = "".join(
            random.choices(string.ascii_letters + string.digits, k=32)
        )
        tid = self.unicom_token_id
        for n, v in [
            ("TOKENID_COOKIE", f"chinaunicom-{tid}"),
            ("UNICOM_TOKENID", tid),
            ("sdkuuid", tid),
        ]:
            self.client.cookies.set(n, v, domain=".10010.com")

    def log(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] [账号{self.idx}] {redact_text(msg)}"
        print(line, flush=True)
        NOTIFY_LINES.append(line)

    def task_log(self, tag, msg):
        line = (
            f"[{datetime.now():%H:%M:%S}] [账号{self.idx}] [{tag}] {redact_text(msg)}"
        )
        print(line, flush=True)
        NOTIFY_LINES.append(line)

    def tlog(self, msg):
        tag = getattr(self, "_task_tag", None)
        if tag:
            self.task_log(tag, msg)
        else:
            self.log(msg)

    def format_exc(self, e, action=None):
        return format_exception_detail(e, action)

    def log_exc(self, e, action=None):
        self.log(f"异常: {self.format_exc(e, action)}")

    def tlog_exc(self, e, action=None):
        self.tlog(f"异常: {self.format_exc(e, action)}")

    async def close(self):
        await self.client.aclose()

    async def req(self, method, url, **kw):
        allow_redirects = kw.pop("allow_redirects", True)
        try:
            r = await self.client.request(
                method, url, follow_redirects=allow_redirects, **kw
            )
        except httpx.RequestError as e:
            path = urlparse(url).path or url
            return {
                "code": -1,
                "headers": {},
                "data": None,
                "error": format_exception_detail(e, f"{method} {path}"),
            }
        body = r.text
        stripped = body.strip()
        if stripped.startswith(("{", "[")):
            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                path = urlparse(url).path or url
                return {
                    "code": r.status_code,
                    "headers": r.headers,
                    "data": None,
                    "error": format_exception_detail(e, f"{method} {path}"),
                }
        else:
            data = body
        return {"code": r.status_code, "headers": r.headers, "data": data}

    async def get_ticket(self, url):
        r = await self.req(
            "GET",
            "https://m.client.10010.com/mobileService/openPlatform/openPlatLineNew.htm",
            params={"to_url": url},
            allow_redirects=False,
        )
        if loc := r["headers"].get("location") or r["headers"].get("Location"):
            # 完整落地页 URL 留给调用方当 Referer（woauth2 授权有 referer 校验）
            self.last_ticket_url = loc
            return parse_qs(urlparse(loc).query).get("ticket", [""])[0]
        return ""

    # === 登录 ===
    async def login(self):
        r = await self.req(
            "POST",
            "https://m.client.10010.com/mobileService/onLine.htm",
            data={
                "token_online": self.cookie,
                "reqtime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "appId": APP_ID,
                "version": APP_VERSION,
                "step": "bindlist",
                "isFirstInstall": 0,
                "deviceModel": "iPhone14,6",
                "uniqueIdentifier": "ios"
                + "".join(random.choices("0123456789abcdef", k=32)),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        res = r["data"]
        if not isinstance(res, dict):
            self.log(f"登录失败: {api_response_err(r, '在线登录')}")
            return False
        if str(res.get("code")) == "0":
            self.mobile, self.ecs_token = (
                str(res.get("desmobile") or ""),
                str(res.get("ecs_token") or ""),
            )
            list_data = res.get("list") if isinstance(res.get("list"), list) else []
            city_info = (
                list_data[0] if list_data and isinstance(list_data[0], dict) else {}
            )
            self.province = str(city_info.get("proName") or "")
            pro_code = str(
                city_info.get("proCode") or city_info.get("standardProvinceCode") or ""
            )
            self.province_code = pro_code.lstrip("0") or pro_code
            self.log(
                f"登录成功: {self.mobile[:3]}****{self.mobile[-4:]} ({self.province})"
            )
            if self.ecs_token:
                self.client.cookies.set(
                    "ecs_token", self.ecs_token, domain=".10010.com"
                )

            return True
        self.log(f"登录失败: {api_response_err(r, '在线登录')}")
        return False

    async def run(self):
        """校验任务配置后登录；每个活动隔离异常，始终关闭账号连接。"""
        try:
            try:
                tasks = select_tasks(
                    os.environ.get("UNICOM_ONLY_TASK", ""),
                    os.environ.get("UNICOM_SKIP_TASK", ""),
                )
            except ValueError as exc:
                self.log_exc(exc, "任务配置")
                return
            if not tasks:
                self.log("没有需要执行的任务")
                return
            if not await self.login():
                return
            cloud_ready = False
            for spec in tasks:
                self._task_tag = spec.label
                try:
                    if spec.needs_cloud and not cloud_ready:
                        await self.cloudpan_login()
                        cloud_ready = True
                    await getattr(self, spec.method)()
                except Exception as exc:
                    self.tlog_exc(exc, spec.method)
                finally:
                    self._task_tag = None
        finally:
            await self.close()


async def main():
    NOTIFY_LINES.clear()  # 进程内重复调用 main() 时不累积上次通知
    # 10点开抢/日期比对/周日判断都依赖本地时区，容器 TZ 为空(含空串)时显式钉死
    if not os.environ.get("TZ"):
        os.environ["TZ"] = "Asia/Shanghai"
    if hasattr(time, "tzset"):
        time.tzset()
    print(f"开始: {datetime.now():%Y-%m-%d %H:%M:%S} TZ={os.environ.get('TZ')}")
    cookies = os.environ.get("chinaUnicomCookie", "")
    accounts = [cookie.strip() for cookie in cookies.split("@") if cookie.strip()]
    if not accounts:
        print("未找到环境变量 chinaUnicomCookie")
        return

    tasks = [Unicom(cookie, idx).run() for idx, cookie in enumerate(accounts, start=1)]
    print(f"启动 {len(tasks)} 个账号")
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            line = f"账号{i + 1} 未捕获异常: {format_exception_detail(result)}"
            print(line, flush=True)
            NOTIFY_LINES.append(line)
    push_lines = [
        line
        for line in NOTIFY_LINES
        if any(keyword in line for keyword in ("失败", "异常", "成功", "抽奖"))
    ]
    if len(push_lines) > 100:  # 通知体积上限
        push_lines = push_lines[:100] + [f"... 其余 {len(push_lines) - 100} 行已省略"]
    if push_lines:
        notify_send("中国联通任务", "\n".join(push_lines))
    print("结束")


if __name__ == "__main__":
    asyncio.run(main())
