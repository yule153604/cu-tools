"""云盘上传大比拼。"""

import asyncio
import time
from datetime import datetime
from urllib.parse import parse_qs, quote, urljoin, urlparse

from cu_tasks.common import (
    BATTLE_UA,
    CLOUD_BATTLE_ACTIVITY_ID,
    CLOUD_BATTLE_FILE_CONTENT,
    CLOUD_BATTLE_FILE_NAME,
    CLOUD_BATTLE_PAGE,
    CLOUD_BATTLE_TOUCHPOINT,
    CLOUD_BATTLE_UPLOAD_URLS,
    require_dict_data,
    require_int,
    response_summary,
    safe_int,
)


class BattleTasks:
    """活动实现，复用宿主的账号会话、请求与日志接口。"""

    # === 11. 云盘上传大比拼（38 期；签名/上传协议与校园季同源）===

    def battle_referer(self):
        if self.battle_page_referer:
            return self.battle_page_referer
        token = self.cloud_disk_token()
        return (
            f"https://panservice.mail.wo.cn/h5/activitymobile/{CLOUD_BATTLE_PAGE}"
            f"?activityId={quote(CLOUD_BATTLE_ACTIVITY_ID)}&type=02"
            f"&touchpoint={CLOUD_BATTLE_TOUCHPOINT}&clientid=1001000003&token={token}"
        )

    async def battle_enter(self):
        """进入活动页，刷新 token 并获取带 ticket 的 Referer（lottery-times 必需）"""
        token = self.cloud_disk_token()
        if not token:
            return False
        entry = (
            f"https://panservice.mail.wo.cn/h5/activitymobile/{CLOUD_BATTLE_PAGE}"
            f"?activityId={quote(CLOUD_BATTLE_ACTIVITY_ID)}&touchpoint={CLOUD_BATTLE_TOUCHPOINT}"
            f"&clientid=1001000003&token={token}"
        )
        r = await self.req(
            "GET",
            "https://m.client.10010.com/mobileService/openPlatform/openPlatLineNew.htm",
            params={"to_url": entry},
            allow_redirects=False,
        )
        url = r["headers"].get("location") or r["headers"].get("Location")
        if not url:
            return False
        for _ in range(4):
            if url.startswith("/"):
                url = urljoin("https://panservice.mail.wo.cn", url)
            self.battle_page_referer = url.split("#", 1)[0]
            q = parse_qs(urlparse(url).query)
            new_token = (q.get("token") or [""])[0]
            if new_token:
                self.cloud_disk = {"userToken": new_token}
            if "ticket=" in url:
                return True
            r = await self.req("GET", url, allow_redirects=False)
            nxt = r["headers"].get("location") or r["headers"].get("Location")
            if not nxt or nxt == url:
                return "ticket=" in url
            url = nxt
        return False

    def battle_headers(self, client_id="1001000003", extra=None):
        token = self.cloud_disk_token()
        if not token:
            return {}
        headers = {
            "X-YP-Access-Token": token,
            "Accept": "application/json, text/plain, */*",
            "source-type": "woapi",
            "requestTime": str(int(time.time() * 1000)),
            "User-Agent": BATTLE_UA,
            "clientId": "1001000165",
            "X-SH-Access-Token": "",
            "X-YP-GRAY-FLAG": "undefined",
            "Content-Type": "application/json",
            "X-YP-Client-Id": client_id,
            "token": token,
            "Origin": "https://panservice.mail.wo.cn",
            "Referer": self.battle_referer(),
        }
        if extra:
            headers.update(extra)
        return headers

    async def battle_post(self, path, payload=None, client_id="1001000003", extra=None):
        headers = self.battle_headers(client_id, extra)
        if not headers:
            return {}
        r = await self.req(
            "POST",
            f"https://panservice.mail.wo.cn{path}",
            headers=headers,
            json=payload or {},
        )
        return require_dict_data(r, path)

    async def battle_get(self, path, params=None, client_id="1001000003", extra=None):
        headers = self.battle_headers(client_id, extra)
        if not headers:
            return {}
        r = await self.req(
            "GET",
            f"https://panservice.mail.wo.cn{path}",
            headers=headers,
            params=params or {},
        )
        return require_dict_data(r, path)

    async def battle_signed_post(
        self, path, key, payload=None, client_id="1001000003", extra=None
    ):
        """getTimestamp 取 nonce/timestamp -> HMAC-SHA256 签名 -> 提交"""
        ts = await self.battle_post(
            "/activity/getTimestamp", {"key": key}, client_id, extra
        )
        result = ts.get("result") or {}
        nonce = result.get("nonce")
        timestamp = result.get("timestamp")
        if not nonce or not timestamp:
            self.tlog(f"getTimestamp失败 {response_summary(ts)}")
            return {}
        body = dict(payload or {})
        body.update(
            {
                "activityId": CLOUD_BATTLE_ACTIVITY_ID,
                "nonce": nonce,
                "timestamp": timestamp,
            }
        )
        body["sign"] = self.cloud_activity_sign(body)
        return await self.battle_post(path, body, client_id, extra)

    async def battle_check_opened(self):
        """返回 True/False 表示是否已开启冲榜；None 表示查询失败。"""
        data = await self.battle_get(
            "/activity/checkActivityStatus", {"activityId": CLOUD_BATTLE_ACTIVITY_ID}
        )
        meta = data.get("meta") or {}
        if str(meta.get("code")) != "200":
            self.tlog(f"查询开启状态失败 {response_summary(data)}")
            return None
        result = data.get("result")
        if not isinstance(result, dict) or "state" not in result:
            self.tlog(f"查询开启状态失败 {response_summary(data)}")
            return None
        try:
            state = require_int(result["state"], "checkActivityStatus.state")
        except ValueError:
            self.tlog(f"查询开启状态失败 {response_summary(data)}")
            return None
        if state not in (0, 1):
            self.tlog(f"查询开启状态失败 {response_summary(data)}")
            return None
        return state == 1

    async def battle_open_activity(self):
        if not self.province or not self.province_code:
            self.tlog("缺少省份信息，无法开启冲榜")
            return False
        data = await self.battle_post(
            "/activity/openActivity",
            {
                "activityId": CLOUD_BATTLE_ACTIVITY_ID,
                "provinceCode": self.province_code,
                "provinceName": self.province,
            },
        )
        meta = data.get("meta") or {}
        if (
            str(meta.get("code")) == "200"
            and safe_int((data.get("result") or {}).get("state")) == 1
        ):
            self.tlog(f"开启冲榜成功 {self.province}")
            return True
        self.tlog(f"开启冲榜失败 {meta.get('message') or response_summary(data)}")
        return False

    async def battle_lottery_times(self):
        """返回 (次数, data)；次数为 None 表示查询失败。"""
        data = await self.battle_get(
            "/activity/lottery/lottery-times", {"activityId": CLOUD_BATTLE_ACTIVITY_ID}
        )
        meta = data.get("meta") or {}
        if str(meta.get("code")) != "200":
            self.tlog(f"查询抽奖次数失败 {response_summary(data)}")
            return None, data
        result = data.get("result")
        try:
            times = require_int(result, "lotteryTimes", minimum=0)
        except ValueError:
            self.tlog(f"查询抽奖次数失败 {response_summary(data)}")
            return None, data
        return times, data

    def battle_drawn_today(self, records):
        today = datetime.now().date()
        for item in records or []:
            if not isinstance(item, dict):
                continue
            create_time = item.get("createTime") or ""
            try:
                if datetime.strptime(create_time[:10], "%Y-%m-%d").date() == today:
                    return True
            except ValueError:
                continue
        return False

    async def battle_upload_file(self):
        """上传 1 个文件完成上传任务（服务端自动记账，无额外上报接口）"""
        content = CLOUD_BATTLE_FILE_CONTENT.encode("utf-8")
        for i, url in enumerate(CLOUD_BATTLE_UPLOAD_URLS):
            ok, msg = await self.cloud_upload2c(url, CLOUD_BATTLE_FILE_NAME, content)
            if ok:
                self.tlog(msg)
                return True
            self.tlog(f"上传失败[{urlparse(url).netloc}] {msg}")
        self.tlog("所有上传域名均失败；可用 UNICOM_BATTLE_UPLOAD_URL 指定域名")
        return False

    async def battle_wait_lottery_times(self, max_wait=8):
        """轮询抽奖次数；返回次数，None 表示查询失败，0 表示合法无次数。"""
        for i in range(max_wait):
            if i:
                await asyncio.sleep(1)
            count, _ = await self.battle_lottery_times()
            if count is None:
                return None
            if count > 0:
                self.tlog(f"抽奖次数 {count}")
                return count
        self.tlog("上传后未获得抽奖次数")
        return 0

    async def battle_province_ranking(self):
        """省份排行（仅展示/校验签名通路；失败只记日志，不阻断上传与抽奖）"""
        data = await self.battle_signed_post(
            "/activity/file/upload/battle/provinceRanking",
            "activity:file:upload:battle:rank",
            {"topN": 34},
        )
        meta = data.get("meta") or {}
        if str(meta.get("code")) != "200":
            self.tlog(
                f"省份排名查询失败 {meta.get('message') or response_summary(data)}"
            )

    async def battle_draw_lottery(self):
        prize = await self.battle_signed_post("/activity/lottery", "activity:lottery")
        meta = prize.get("meta") or {}
        if str(meta.get("code")) != "200":
            self.tlog(f"抽奖失败 {meta.get('message') or response_summary(prize)}")
            return False
        result = prize.get("result") or {}
        prize_name = result.get("prizeName") or response_summary(prize)
        self.tlog(f"抽奖成功 {prize_name}")
        return True

    async def cloud_battle_task(self):
        """上传大比拼：进入活动 -> 开启冲榜 -> 上传文件 -> 抽奖"""
        self._task_tag = "上传大比拼"
        if not self.cloud_disk_token():
            self.tlog("缺少云盘 userToken, 跳过")
            return
        try:
            if not await self.battle_enter():
                self.tlog("进入活动页失败")
                return

            status = await self.battle_get(
                "/activity/activity-status", {"activityId": CLOUD_BATTLE_ACTIVITY_ID}
            )
            if str((status.get("meta") or {}).get("code")) != "200":
                self.tlog(f"查询活动状态失败 {response_summary(status)}")
                return
            status_result = status.get("result")
            if (
                not isinstance(status_result, dict)
                or "activityStatus" not in status_result
            ):
                self.tlog(
                    f"查询活动状态失败 响应缺少activityStatus {response_summary(status)}"
                )
                return
            try:
                activity_status = require_int(
                    status_result.get("activityStatus"), "activityStatus"
                )
            except ValueError:
                self.tlog(
                    f"查询活动状态失败 activityStatus非法 {response_summary(status)}"
                )
                return
            if activity_status != 1:
                self.tlog(
                    "活动未上线或已结束；若活动已换期，"
                    "请设置 UNICOM_BATTLE_ACTIVITY_ID 为新 ID"
                )
                return

            opened = await self.battle_check_opened()
            if opened is None:
                return
            if not opened:
                if not await self.battle_open_activity():
                    return
            else:
                self.tlog("冲榜已开启")

            records = await self.battle_get(
                "/activity/lottery/recordList", {"activityId": CLOUD_BATTLE_ACTIVITY_ID}
            )
            if str((records.get("meta") or {}).get("code")) != "200":
                self.tlog(f"查询抽奖记录失败 {response_summary(records)}")
                return
            # 无中奖记录时接口不返回 result 字段（前端同款容错: e.result || []）
            record_list = records.get("result") or []
            if not isinstance(record_list, list):
                self.tlog(f"查询抽奖记录失败 result非list {response_summary(records)}")
                return
            if self.battle_drawn_today(record_list):
                self.tlog("今日已抽奖")
                return

            # 排行榜仅展示用，失败不阻断上传与抽奖
            await self.battle_province_ranking()

            times, _ = await self.battle_lottery_times()
            if times is None:
                return
            if times <= 0:
                if not await self.battle_upload_file():
                    # 服务端可能已收到上传（60s ReadTimeout 后仍计数），不能直接放弃
                    self.tlog("上传疑似超时，仍尝试查询抽奖次数")
                times = await self.battle_wait_lottery_times()
                if times is None or times <= 0:
                    return
            else:
                self.tlog(f"抽奖次数 {times}")

            await self.battle_draw_lottery()
        except Exception as e:
            self.tlog_exc(e, "上传大比拼")
