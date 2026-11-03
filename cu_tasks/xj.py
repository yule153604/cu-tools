"""新疆联通每月主题活动。"""

import asyncio
from datetime import datetime

from cu_tasks.common import (
    UA,
)


class XinjiangTasks:
    """活动实现，复用宿主的账号会话、请求与日志接口。"""

    async def xj_task(self):
        self._task_tag = "新疆联通"
        if "新疆" not in self.province:
            return
        self.tlog("开始")
        ticket = await self.get_ticket(
            "https://zy100.xj169.com/touchpoint/openapi/jumpHandRoom1G?source=155&type=02"
        )
        if not ticket:
            return
        # 9 月改版后 draw 接口有会话校验：必须先访问活动落地页，让服务端下发会话 cookie
        if self.last_ticket_url:
            await self.req("GET", self.last_ticket_url, headers={"User-Agent": UA})
        # 完整落地页 URL 作 Referer（9 月改版后 draw 系列接口校验 referer/origin）
        xj_referer = self.last_ticket_url or (
            "https://zy100.xj169.com/touchpoint/openapi/jumpHandRoom1G"
            f"?source=155&type=02&ticket={ticket}"
        )

        # 获取token
        r = await self.req(
            "POST",
            "https://zy100.xj169.com/touchpoint/openapi/getTokenAndCity",
            headers={
                "Referer": xj_referer,
                "Origin": "https://zy100.xj169.com",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            },
            data={"ticket": ticket},
        )

        token = None
        if (res := r["data"]) and isinstance(res, dict):
            if res.get("code") == 0:
                token = res.get("data", {}).get("token")

        if not token:
            return
        self.tlog("登录成功")

        # 获取活动ID
        month_abbr = [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
        now = datetime.now()
        # 10 月新版链路：主题活动 ID 带 Theme 前缀（ThemeOct2026Act），接口 URL 后缀仍为 Oct2026Act；
        # 打卡活动 ID 为 dakaThemeOct2026Act
        act_suffix = f"{month_abbr[now.month - 1]}{now.year}Act"
        month_activity_id = f"Theme{act_suffix}"

        # 获取commHighFlag（服务端要求小写字符串 true/false）
        xj_comm_high_flag = "false"
        r = await self.req(
            "POST",
            "https://zy100.xj169.com/touchpoint/openapi/themeAct/commonHightRest",
            headers={
                "userToken": token,
                "Referer": xj_referer,
                "Origin": "https://zy100.xj169.com",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            },
            data={"activityId": month_activity_id},
        )
        if (
            (res := r["data"])
            and isinstance(res, dict)
            and res.get("code") in [0, "SUCCESS"]
        ):
            raw_flag = res.get("data", False)
            xj_comm_high_flag = (
                "true"
                if raw_flag is True or str(raw_flag).lower() == "true"
                else "false"
            )

        # 每月主题抽奖
        r = await self.req(
            "POST",
            f"https://zy100.xj169.com/touchpoint/openapi/themeAct/draw_{act_suffix}",
            headers={
                "userToken": token,
                "Referer": xj_referer,
                "Origin": "https://zy100.xj169.com",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            },
            data={
                "activityId": month_activity_id,
                "prizeId": "",
                "commHighFlag": xj_comm_high_flag,
            },
        )
        if (res := r["data"]) and isinstance(res, dict):
            msg, data = res.get("msg"), res.get("data")
            if msg == "thanks1":
                self.tlog(f"每月抽奖: {data}")
            elif res.get("code") in [0, "SUCCESS"]:
                self.tlog(f"每月抽奖: {msg or data}")
                # 中奖后与活动页一致：下发短信
                prize_code = str(msg or "")
                if prize_code and prize_code != "thanks1":
                    await self.req(
                        "POST",
                        "https://zy100.xj169.com/touchpoint/openapi/marchAct/draw_SMS",
                        headers={
                            "userToken": token,
                            "Referer": xj_referer,
                            "Origin": "https://zy100.xj169.com",
                            "X-Requested-With": "XMLHttpRequest",
                            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                        },
                        data={
                            "activityId": month_activity_id,
                            "prizeId": prize_code,
                        },
                    )
            elif data and (
                "已经打过卡" in str(data)
                or "机会已用完" in str(data)
                or "已达上限" in str(data)
            ):
                self.tlog(f"每月抽奖: {data}")
            else:
                self.tlog(f"每月抽奖: {data or msg}")

        # 每日打卡抽奖（页面上有两个打卡按钮，逐个尝试）
        for idx, daka_prize in enumerate(
            ("daka_sftczxqb_twenty", "daka_mtwmhblly"), start=1
        ):
            await asyncio.sleep(1)
            r = await self.req(
                "POST",
                f"https://zy100.xj169.com/touchpoint/openapi/themeAct/draw_{act_suffix}",
                headers={
                    "userToken": token,
                    "Referer": xj_referer,
                    "Origin": "https://zy100.xj169.com",
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                },
                data={
                    "activityId": f"daka{month_activity_id}",
                    "prizeId": daka_prize,
                    "commHighFlag": xj_comm_high_flag,
                },
            )
            if (res := r["data"]) and isinstance(res, dict):
                msg, data = res.get("msg"), res.get("data")
                if msg == "thanks1":
                    self.tlog(f"每日打卡{idx}: {data}")
                elif res.get("code") in [0, "SUCCESS"]:
                    self.tlog(f"每日打卡{idx}: {msg or data}")
                elif res.get("msgType") == "200":
                    self.tlog(f"每日打卡{idx}: 已打过卡")
                elif data and ("已经打过卡" in str(data) or "机会已用完" in str(data)):
                    self.tlog(f"每日打卡{idx}: {data}")
                else:
                    self.tlog(f"每日打卡{idx}: {data or msg}")
