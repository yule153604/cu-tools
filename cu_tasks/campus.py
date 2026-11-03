"""云盘校园季与内容转存。"""

import asyncio
import hashlib
import json
import random
import re
import time
from datetime import datetime
from urllib.parse import quote

from cu_tasks.common import (
    BATTLE_UA,
    CAMPUS_EDU_FALLBACK_ID,
    CAMPUS_ENT_FALLBACK_ID,
    CAMPUS_FALLBACK_NAME,
    WO_VIDEO_BASE,
    WO_VIDEO_CHANNEL,
    require_int,
    response_summary,
    wo_aes_b64,
    wo_aes_unb64,
    wo_des_b64,
)


class CampusTasks:
    """活动实现，复用宿主的账号会话、请求与日志接口。"""

    CAMPUS_ACTIVITY_ID = "MzU="
    CAMPUS_UPLOAD_URL = "https://tjupload.pan.wo.cn/openapi/client/upload2C"

    def campus_headers(self):
        token = self.cloud_disk_token()
        return {
            "X-YP-Access-Token": token,
            "token": token,
            "Access-Token": token,
            "source-type": "woapi",
            "clientId": "1001000165",
            "X-YP-Client-Id": "1001000003",
            "Content-Type": "application/json",
            "requestTime": str(int(time.time() * 1000)),
            "User-Agent": BATTLE_UA,
            "Origin": "https://panservice.mail.wo.cn",
            "Referer": (
                "https://panservice.mail.wo.cn/h5/activitymobile/campusSeason"
                f"?activityId={quote(self.CAMPUS_ACTIVITY_ID)}&type=02"
                f"&token={token}&clientid=1001000003"
            ),
        }

    async def campus_signed_post(self, path, key, extra=None):
        """取时间戳 -> 签名 -> 提交校园季活动接口"""
        ts = await self.req(
            "POST",
            "https://panservice.mail.wo.cn/activity/getTimestamp",
            headers=self.campus_headers(),
            json={"key": key},
        )
        result = (ts.get("data") or {}).get("result") or {}
        nonce, timestamp = result.get("nonce"), result.get("timestamp")
        if not nonce or not timestamp:
            self.tlog(f"getTimestamp 失败 {response_summary(ts)}")
            return {}
        body = {"activityId": self.CAMPUS_ACTIVITY_ID, **(extra or {})}
        body["nonce"] = nonce
        body["timestamp"] = timestamp
        body["sign"] = self.cloud_activity_sign(body)
        r = await self.req(
            "POST",
            f"https://panservice.mail.wo.cn{path}",
            headers=self.campus_headers(),
            json=body,
        )
        return r.get("data") or {}

    async def campus_upload(self):
        """上传 1 字节 1.txt 完成上传任务"""
        return await self.cloud_upload2c(
            self.CAMPUS_UPLOAD_URL,
            "1.txt",
            b"1",
            referer="https://panservice.mail.wo.cn/",
        )

    # --- 沃视频内容转存（校园季"转存教育内容"/"转存娱乐盘内容"任务） ---

    WO_VIDEO_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) LianTongYunPan/6.2.0 (iPhone; iOS 27.0)"
    )

    def pan_app_headers(self):
        """s.pan.wo.cn 竖版 App 请求头（ticket / 校园 tab 用）。"""
        token = self.cloud_disk_token()
        return {
            "X-YP-Access-Token": token,
            "Access-Token": token,
            "Client-Id": "1001000035",
            "app-type": "liantongyunpanapp",
            "App-Version": "yp-app/6.2.0",
            "bundleid": "com.chinaunicom.bol.cloud",
            "User-Agent": "LianTongYunPan/6.2.0 (iPhone;iOS 27.0)",
            "Content-Type": "application/json",
        }

    async def wo_ticket(self):
        """取云盘 ticket（进沃视频详情页的通行证）。"""
        r = await self.req(
            "GET",
            "https://s.pan.wo.cn/api-user/api/user/ticket",
            headers=self.pan_app_headers(),
        )
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        return str(result.get("ticket") or "")

    async def wo_enter(self, content_id, ticket):
        """打开沃视频详情页，换取 mbh 侧会话；返回 (Referer, h5Token)。"""
        ua = {"User-Agent": self.WO_VIDEO_UA}
        referer = (
            f"{WO_VIDEO_BASE}/YunPanVideoV4/index.html?id={content_id}"
            f"&ticket={quote(ticket)}"
        )
        await self.req("GET", referer, headers=ua)
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        sign = (
            hashlib.md5(f"{ticket}dncj@2181{ts}spamx1a7s".encode()).hexdigest().upper()
        )
        await self.req(
            "POST",
            f"{WO_VIDEO_BASE}/TjCenter/wovideo/h5/getCookie",
            headers={**ua, "Content-Type": "application/json"},
            json={"timestamp": ts, "md5": sign, "mobile": ticket, "clientType": "3"},
        )
        info = await self.req(
            "GET",
            f"{WO_VIDEO_BASE}/TjCenter/wovideo/check/getUserInfoByCookie",
            headers=ua,
        )
        data = info.get("data") if isinstance(info.get("data"), dict) else {}
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        return referer, str(inner.get("h5Token") or "")

    async def wo_dispatcher(self, api_key, body, referer):
        """POST /WoYunPanA/woapi/dispatcher（body AES 加密），返回 RSP。"""
        r = await self.req(
            "POST",
            f"{WO_VIDEO_BASE}/WoYunPanA/woapi/dispatcher",
            headers={
                "User-Agent": self.WO_VIDEO_UA,
                "Content-Type": "application/json",
                "Origin": WO_VIDEO_BASE,
                "Referer": referer,
                "key": api_key,
                "channel": WO_VIDEO_CHANNEL,
            },
            json={
                "header": {"key": api_key, "channel": WO_VIDEO_CHANNEL},
                "body": wo_aes_b64(
                    json.dumps(body, ensure_ascii=False, separators=(",", ":"))
                ),
            },
        )
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        rsp = data.get("RSP")
        if str(data.get("STATUS")) != "200" or not isinstance(rsp, dict):
            return {}
        return rsp

    async def wo_transfer_status(self, media_id, referer):
        """转存状态：True=已转存，False=未转存，None=查询失败。"""
        rsp = await self.wo_dispatcher(
            "JudgeFileTransferStatus",
            {"userId": self.mobile, "fileUniqueValue": wo_des_b64(media_id)},
            referer,
        )
        if str(rsp.get("RSP_CODE")) != "0000":
            return None
        try:
            return wo_aes_unb64(str(rsp.get("DATA") or "")) != "0"
        except Exception:
            return None

    async def wo_file_transfer(
        self, main_id, media_id, name, file_size, referer, space_type=None
    ):
        """WoFileTransfer 提交转存；返回 (是否成功, 说明)。"""
        now = datetime.now().strftime("%Y%m%d%H%M%S")
        detail = {
            "mainContentId": main_id,
            "contentId": media_id,
            "userId": self.mobile,
            "time": now,
        }
        body = {
            "userId": self.mobile,
            "clientId": WO_VIDEO_CHANNEL,
            "fileName": name,
            "fileSize": int(file_size or 0)
            or random.randint(1024 * 1024, 2 * 1024 * 1024),
            "fileUniqueValue": wo_des_b64(media_id),
            "fileDetailLink": wo_des_b64(
                json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
            ),
        }
        if space_type:
            body["spaceType"] = str(space_type)
        rsp = await self.wo_dispatcher("WoFileTransfer", body, referer)
        code = str(rsp.get("RSP_CODE") or "")
        if code == "0000":
            return True, "成功"
        return False, f"RSP_CODE={code or '无'}"

    async def wo_content_package(self, content_id):
        """兼容单集调用方；教育频道扫描使用完整候选列表。"""
        episodes = await self.wo_content_package_items(content_id)
        return episodes[0] if episodes else None

    async def wo_content_package_items(self, content_id):
        """查课程包，返回最多 50 集候选，避免只扫描每门课的第一集。"""
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        signature = (
            hashlib.sha256(
                f"foQ9ma89UoOkDMFs{self.mobile}{content_id}{ts}".encode()
            ).hexdigest()
            + ts
        )
        r = await self.req(
            "POST",
            f"{WO_VIDEO_BASE}/GW/queryContentPackageDetailForWV",
            headers={
                "User-Agent": self.WO_VIDEO_UA,
                "Content-Type": "application/json",
            },
            json={
                "userId": self.mobile,
                "contentId": content_id,
                "count": 50,
                "offset": 0,
                "accessChannel": "91",
                "signature": signature,
            },
        )
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        detail = (
            data.get("contentPackageDetail")
            if isinstance(data.get("contentPackageDetail"), dict)
            else {}
        )
        episodes = (
            detail.get("mobVodDetails")
            if isinstance(detail.get("mobVodDetails"), list)
            else []
        )
        candidates = []
        seen = set()
        for ep in episodes[:50]:
            if not isinstance(ep, dict):
                continue
            media_id = str(ep.get("id") or ep.get("code") or "")
            if not media_id or media_id in seen:
                continue
            seen.add(media_id)
            name = re.sub(r"\s+", "", str(ep.get("name") or ""))
            files = ep.get("mediaFiles")
            sizes = (
                files[0].get("fileSize")
                if isinstance(files, list) and files and isinstance(files[0], dict)
                else None
            )
            try:
                size = (
                    require_int(sizes[0], "fileSize", minimum=0)
                    if isinstance(sizes, list) and sizes
                    else 0
                )
            except ValueError:
                size = 0
            candidates.append((media_id, name, size))
        return candidates

    async def wo_authorize_edu(self, h5_token, referer):
        """教育产品授权（转存教育内容前置，失败不阻塞）。"""
        code = quote(quote(wo_des_b64("authType=1", "8wrap2ih"), safe=""), safe="")
        r = await self.req(
            "POST",
            f"{WO_VIDEO_BASE}/TjCenter/cloudDrive/authorizeEduProduct",
            params={"code": code, "platform": "h5"},
            headers={
                "User-Agent": self.WO_VIDEO_UA,
                "h5-token": h5_token,
                "Referer": referer,
            },
            json={},
        )
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        return str(data.get("status")) == "0" and inner.get("isSuccess") == 1

    async def campus_tab_videos(self, tab_code):
        """校园 tab 内指向沃视频详情页的内容 [(content_id, title)]。"""
        r = await self.req(
            "GET",
            "https://s.pan.wo.cn/api/bff/home/school/v2/tab/content",
            params={"tabCode": tab_code},
            headers=self.pan_app_headers(),
        )
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        sections = data.get("result") if isinstance(data.get("result"), list) else []
        videos = []
        for section in sections:
            items = section.get("data") if isinstance(section, dict) else None
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                m = re.search(
                    r"YunPanVideoV4/index\.html\?id=([0-9A-Za-z]+)",
                    str(item.get("jumpUrl") or ""),
                )
                if m and m.group(1) not in {v[0] for v in videos}:
                    videos.append((m.group(1), str(item.get("title") or "")))
        return videos

    async def campus_transfer_targets(self, kind):
        """候选转存内容（校园 tab 动态发现 + 兜底 id）。

        返回 [(main_id, media_id, name, file_size, space_type)]。
        """
        tab = "school_tab_learning" if kind == "edu" else "school_tab_entertainment"
        targets = []
        for content_id, title in await self.campus_tab_videos(tab):
            name = re.sub(r"\s+", "", title)
            if kind == "edu":
                ep = await self.wo_content_package(content_id)
                if ep:
                    targets.append((content_id, ep[0], ep[1], ep[2], "76"))
                    continue
                targets.append((content_id, content_id, name, 0, "76"))
            else:
                targets.append((content_id, content_id, name, 0, None))
        fallback = CAMPUS_EDU_FALLBACK_ID if kind == "edu" else CAMPUS_ENT_FALLBACK_ID
        if fallback:
            ep = await self.wo_content_package(fallback) if kind == "edu" else None
            if ep:
                targets.append((fallback, ep[0], ep[1], ep[2], "76"))
            else:
                targets.append(
                    (
                        fallback,
                        fallback,
                        CAMPUS_FALLBACK_NAME[kind],
                        0,
                        "76" if kind == "edu" else None,
                    )
                )
        return targets[:12]  # 候选过多时只试前几个，避免无谓耗时

    async def campus_transfer(self, kind):
        """校园季转存任务：kind=edu 转存教育内容 / ent 转存娱乐盘内容。"""
        label = "教育" if kind == "edu" else "娱乐"
        targets = await self.campus_transfer_targets(kind)
        if not targets:
            self.tlog(f"转存{label}: 未找到可转存内容")
            return False
        skipped = None
        for main_id, media_id, name, size, space_type in targets:
            ticket = await self.wo_ticket()
            if not ticket:
                self.tlog(f"转存{label}: ticket 获取失败")
                return False
            referer, h5_token = await self.wo_enter(main_id, ticket)
            if kind == "edu" and h5_token:
                await self.wo_authorize_edu(h5_token, referer)
            status = await self.wo_transfer_status(media_id, referer)
            if status is None:
                self.tlog(f"转存{label}: [{name}] 状态查询失败, 试下一个")
                continue
            if status:
                skipped = skipped or (
                    main_id,
                    media_id,
                    name,
                    size,
                    space_type,
                    referer,
                )
                continue
            ok, msg = await self.wo_file_transfer(
                main_id, media_id, name, size, referer, space_type
            )
            if ok:
                self.tlog(f"转存{label}: [{name}] 成功")
                return True
            self.tlog(f"转存{label}: [{name}] 失败 {msg}")
        if skipped:
            # 候选都已转存过：重转一条碰运气（服务端裁决，可能计入当日任务）
            main_id, media_id, name, size, space_type, referer = skipped
            ok, msg = await self.wo_file_transfer(
                main_id, media_id, name, size, referer, space_type
            )
            self.tlog(
                f"转存{label}: [{name}] 已转存过"
                + ("，重复转存已计入" if ok else f"（服务端未计入: {msg}）")
            )
            if ok:
                return True
        if kind == "ent":
            # mbh 渠道无新内容 → 芒果TV（需会员）→ mbh 影视库（不限会员）
            ok, info = await self.mgtv_transfer()
            if ok:
                self.tlog(f"转存{label}: 芒果TV内容源 成功 [{info}]")
                return True
            self.tlog(f"转存{label}: 芒果TV内容源 {info}")
            ok, info = await self.mbh_channel_transfer(self.MBH_MOVIE_SUBJECTS)
            self.tlog(
                f"转存{label}: mbh影视库 "
                + (f"成功 [{info}]" if ok else f"未找到可用内容: {info}")
            )
            return ok
        # 教育：mbh 教育频道兜底（学习盘课程耗尽后）
        ok, info = await self.mbh_channel_transfer(
            self.MBH_EDU_SUBJECTS, space_type="76", package_mode=True
        )
        self.tlog(
            f"转存{label}: mbh教育频道 "
            + (f"成功 [{info}]" if ok else f"未找到可用内容: {info}")
        )
        return ok

    # --- 芒果TV(mgtv) 内容转存：娱乐盘任务长期内容源 ---

    MGTV_ACT_API = "https://mgcact.api.mgtv.com"
    MGTV_CLUB_API = "https://mgcclub.api.mgtv.com"
    MGTV_CLUB_ACTIVITY = "d5b02f34be217c4d"
    MGTV_FALLBACK_HIDS = ("896231", "867929", "859271")

    def mgtv_headers(self):
        """芒果侧请求头。"""
        return {
            "User-Agent": self.WO_VIDEO_UA,
            "Origin": "https://pop.mgtv.com",
            "Referer": "https://pop.mgtv.com/",
        }

    async def mgtv_session(self):
        """用云盘 ticket 换 mgtv 会话 ticket。"""
        ts = await self.req(
            "GET",
            "https://s.pan.wo.cn/api-user/api/user/ticket",
            headers=self.pan_app_headers(),
        )
        ticket = ((ts.get("data") or {}).get("result") or {}).get("ticket")
        if not ticket:
            return ""
        r = await self.req(
            "GET",
            f"{self.MGTV_ACT_API}/api/cu/login",
            params={"ticket": ticket, "t": int(time.time() * 1000)},
            headers=self.mgtv_headers(),
        )
        data = (r.get("data") or {}).get("data") or {}
        return str(data.get("ticket") or "")

    async def mgtv_hids(self, st):
        """club/homepage 取全部可转存内容 hid（约 140 档）。"""
        r = await self.req(
            "POST",
            f"{self.MGTV_CLUB_API}/api/mgc/club/homepage",
            headers={**self.mgtv_headers(), "Content-Type": "application/json"},
            json={
                "ticket": st,
                "t": int(time.time() * 1000),
                "activityId": self.MGTV_CLUB_ACTIVITY,
                "isMgApp": 0,
            },
        )
        data = (r.get("data") or {}).get("data") or {}
        hids = []
        for model in data.get("list") or []:
            for item in model.get("modelDataList") or []:
                hid = str(item.get("words1") or "").strip()
                if hid.isdigit() and hid not in hids:
                    hids.append(hid)
        return hids or list(self.MGTV_FALLBACK_HIDS)

    async def mgtv_episodes(self, st, hid):
        """encrypt + episodes：返回 (hcode, 集列表)。"""
        r = await self.req(
            "POST",
            f"{self.MGTV_ACT_API}/api/cu/mediaId/encrypt",
            headers={**self.mgtv_headers(), "Content-Type": "application/json"},
            json={
                "ticket": st,
                "t": int(time.time() * 1000),
                "hid": str(hid),
                "vids": "1",
            },
        )
        hcode = ((r.get("data") or {}).get("data") or {}).get("hcode")
        if not hcode:
            return "", []
        r2 = await self.req(
            "GET",
            f"{self.MGTV_ACT_API}/api/cu/media/episodes",
            params={"ticket": st, "t": int(time.time() * 1000), "mediaId": hcode},
            headers=self.mgtv_headers(),
        )
        eps = (r2.get("data") or {}).get("data")
        return str(hcode), eps if isinstance(eps, list) else []

    async def mgtv_transfer_ep(self, st, hcode, ep):
        """转存单个分集。"""
        r = await self.req(
            "POST",
            f"{self.MGTV_ACT_API}/api/cu/woFileTransfer",
            headers={**self.mgtv_headers(), "Content-Type": "application/json"},
            json={
                "ticket": st,
                "t": int(time.time() * 1000),
                "fileName": str(ep.get("title") or "视频"),
                "clipId": hcode,
                "partId": str(ep.get("vcode") or ""),
                "fileType": "2",
            },
        )
        return r.get("data") or {}

    async def mgtv_transfer(self, max_tries=15):
        """扫描芒果内容池，转存一集未转存内容；返回 (是否成功, 说明)。"""
        st = await self.mgtv_session()
        if not st:
            return False, "mgtv 会话获取失败"
        hids = list(await self.mgtv_hids(st))
        random.shuffle(hids)  # 每次随机顺序，避免总扫前面已耗尽的档
        tries = 0
        for hid in hids[:24]:  # 单次运行最多扫 24 档
            hcode, eps = await self.mgtv_episodes(st, hid)
            if not eps:
                continue
            free = [e for e in eps if int(e.get("isPay") or 0) == 0]
            paid = [e for e in eps if int(e.get("isPay") or 0) != 0][:3]
            for ep in free + paid:
                if tries >= max_tries:
                    return False, f"尝试 {tries} 集均未成功"
                tries += 1
                rsp = await self.mgtv_transfer_ep(st, hcode, ep)
                code = str(rsp.get("code") or "")
                if code == "0000":
                    return True, str(ep.get("title") or "")[:40]
                if "无会员权益" in str(rsp.get("msg") or ""):
                    return False, "账号无芒果会员, 跳过芒果内容源"
        return False, f"扫描 {tries} 集均未成功"

    # --- mbh 频道内容源（不限会员，电影/课程）---

    MBH_MOVIE_SUBJECTS = ("catauto1111125306", "catauto1111125307")
    MBH_EDU_SUBJECTS = ("catauto1111134283", "catauto1111134308")

    async def mbh_channel_items(self, subjects, count=50):
        """拉 mbh 频道内容列表，返回 [(code, name)]。"""
        r = await self.req(
            "POST",
            f"{WO_VIDEO_BASE}/jsp/V3/QueryVODAndChannelBatch",
            headers={
                "User-Agent": self.WO_VIDEO_UA,
                "Content-Type": "application/json",
            },
            json={
                "subjects": [
                    {"woSubjectID": s, "count": count, "offset": 0} for s in subjects
                ],
                "versionName": "8.1.0",
            },
        )
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        items = []
        for channel in data.get("contents") or []:
            if not isinstance(channel, dict):
                continue
            for v in channel.get("vc") or []:
                content = v.get("woContent") if isinstance(v, dict) else None
                if not isinstance(content, dict):
                    continue
                code = str(content.get("code") or "")
                name = str(content.get("name") or "")
                if code and name and (code, name) not in items:
                    items.append((code, name))
        return items

    async def mbh_channel_transfer(
        self, subjects, space_type=None, package_mode=False, max_tries=20
    ):
        """按集扫描未转存内容；查询和转存总候选数均受 max_tries 限制。"""
        max_tries = require_int(max_tries, "max_tries", minimum=0)
        if max_tries == 0:
            return False, "扫描上限为 0"
        items = await self.mbh_channel_items(subjects)
        if not items:
            return False, "频道列表为空或获取失败"
        random.shuffle(items)
        tried = already = unknown = rejected = 0
        seen = set()
        for code, name in items[:max_tries]:
            candidates = (
                await self.wo_content_package_items(code)
                if package_mode
                else [(code, name, 0)]
            )
            # 课程列表缺失时不能把课程包 ID 当成可转存的单集 ID。
            if not candidates:
                continue
            ticket = await self.wo_ticket()
            if not ticket:
                return False, "ticket 获取失败"
            referer, h5_token = await self.wo_enter(code, ticket)
            if package_mode and h5_token:
                await self.wo_authorize_edu(h5_token, referer)
            for media_id, file_name, file_size in candidates:
                if media_id in seen:
                    continue
                if tried >= max_tries:
                    break
                seen.add(media_id)
                tried += 1
                status = await self.wo_transfer_status(media_id, referer)
                if status is True:
                    already += 1
                    continue
                if status is not False:
                    unknown += 1
                    continue
                ok, _ = await self.wo_file_transfer(
                    code, media_id, file_name, file_size, referer, space_type
                )
                if ok:
                    return True, file_name[:40]
                rejected += 1
            if tried >= max_tries:
                break
        return (
            False,
            f"扫描 {tried} 集：已转存 {already}，状态查询失败 {unknown}，转存未成功 {rejected}",
        )

    # --- 学习助手 AI 对话（"体验学习助手"任务 30008）---

    AI_WORKFLOW_REFERER = (
        "https://panservice.mail.wo.cn/h5/wocloud_ai_1/workFlow?activeTab=2"
    )

    def ai_headers(self):
        """沃云盘 AI 工作流请求头。"""
        token = self.cloud_disk_token()
        return {
            "X-YP-Access-Token": token,
            "token": token,
            "accesstoken": token,
            "Access-Token": token,
            "source-type": "woapi",
            "clientId": "1001000035",
            "Client-Id": "1001000035",
            "X-YP-Client-Id": "1001000035",
            "X-YP-App-Version": "6.2.0",
            "X-YP-Open-Version": "v1.0",
            "Content-Type": "application/json",
            "requestTime": str(int(time.time() * 1000)),
            "User-Agent": self.WO_VIDEO_UA,
            "Origin": "https://panservice.mail.wo.cn",
            "Referer": self.AI_WORKFLOW_REFERER,
        }

    async def campus_ai_chat(self):
        """体验学习助手：发起一次 AI 对话（SSE 流式），返回是否成功。
        总是新建会话，不查询/复用已有会话，避免污染用户聊天记录。"""
        conv_id = ""
        body = {
            "input": "你好，请用一句话介绍一下你自己",
            "modelId": 0,
            "platform": 2,
            "tag": 0,
            "conversationId": conv_id,
            "knowledgeId": "",
            "referFileInfo": [],
            "messageId": "",
            "conversationType": "bot",
            "recipient": "",
            "aiExtra": "",
            "async": "1",
        }
        headers = {**self.ai_headers(), "Accept": "text/event-stream"}
        url = "https://panservice.mail.wo.cn/wohome/ai/assistant/query"
        bytes_read = 0

        async def read_stream():
            nonlocal bytes_read
            async with self.client.stream(
                "POST", url, headers=headers, json=body, timeout=120.0
            ) as r:
                if r.status_code != 200:
                    return f"HTTP {r.status_code}"
                async for chunk in r.aiter_bytes():
                    bytes_read += len(chunk)
            return ""

        try:
            err = await asyncio.wait_for(read_stream(), timeout=90)
        except asyncio.TimeoutError:
            self.tlog(f"AI对话: 流读取超时(90 秒), 已收 {bytes_read} 字节")
            return bytes_read > 0
        except Exception as e:
            self.tlog(f"AI对话: 异常 {self.format_exc(e)}")
            return False
        if err:
            self.tlog(f"AI对话: 失败 {err}")
            return False
        self.tlog(f"AI对话: 完成 (会话 {conv_id or '新建'}, 流 {bytes_read} 字节)")
        return True

    async def campus_task_progress(self):
        """严格解析任务进度；查询失败不能视为所有任务都未完成。"""
        body = await self.campus_signed_post(
            "/activity/school/task/list", "activity:school:activate"
        )
        meta = body.get("meta") if isinstance(body, dict) else None
        result = body.get("result") if isinstance(body, dict) else None
        tasks = result.get("taskList") if isinstance(result, dict) else None
        if (
            not isinstance(meta, dict)
            or str(meta.get("code")) != "200"
            or not isinstance(tasks, list)
        ):
            raise ValueError(f"校园季任务进度查询失败: {response_summary(body)}")
        progress = {}
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError("校园季任务列表格式错误")
            code = str(task.get("taskCode") or "")
            if code in ("30004", "30005", "30007", "30008"):
                progress[code] = (
                    require_int(task.get("doneCount"), "doneCount", minimum=0),
                    require_int(task.get("dailyLimit"), "dailyLimit", minimum=0),
                )
        return progress

    async def campus_upload_checked(self, before):
        """上传结果不确定时读回活动进度，不盲目重发上传。"""
        ok, message = await self.campus_upload()
        if ok:
            self.tlog(message)
            await asyncio.sleep(3)
            return True
        self.tlog(f"上传响应未确认: {message}；稍后核对任务进度")
        await asyncio.sleep(3)
        try:
            progress = await self.campus_task_progress()
            after = progress.get("30004")
            if after and after[0] > before:
                self.tlog(f"上传任务进度已增加: {before} → {after[0]}，不再重复上传")
                return True
        except ValueError as exc:
            self.tlog_exc(exc, "上传进度核对")
        self.tlog("上传结果仍未确认，本轮停止上传，保留后续转存与抽奖流程")
        return False

    async def campus_task(self):
        """校园季: 激活 -> 上传 -> 查进度 -> 自动抽奖"""
        self._task_tag = "校园季"
        if not self.cloud_disk_token():
            self.tlog("缺少云盘 userToken, 跳过")
            return
        try:
            r = await self.req(
                "POST",
                "https://panservice.mail.wo.cn/activity/task/activate",
                headers=self.campus_headers(),
                json={"activityId": self.CAMPUS_ACTIVITY_ID},
            )
            d = r.get("data") or {}
            if str((d.get("meta") or {}).get("code")) == "200":
                self.tlog("激活成功")
            else:
                self.tlog(f"激活失败 {response_summary(d or r)}")
                return

            progress = await self.campus_task_progress()
            labels = {
                "30004": "上传内容",
                "30005": "转存教育内容",
                "30007": "转存娱乐内容",
                "30008": "体验学习助手",
            }
            done = {}
            for code, (count, limit) in progress.items():
                done[code] = count >= limit
                self.tlog(
                    f"任务[{labels[code]}]: {count}/{limit}"
                    + (" (已满)" if done[code] else "")
                )

            if done.get("30004", True):
                self.tlog("上传任务已满或未返回, 跳过")
            else:
                await self.campus_upload_checked(progress["30004"][0])

            # 转存任务（教育/娱乐各 1 条）——独立于上传，失败只记录不阻断抽奖
            for kind, tcode in (("edu", "30005"), ("ent", "30007")):
                if done.get(tcode, True):
                    self.tlog(
                        "教育转存任务已满, 跳过"
                        if kind == "edu"
                        else "娱乐转存任务已满, 跳过"
                    )
                    continue
                try:
                    await self.campus_transfer(kind)
                except Exception as e:
                    self.tlog_exc(e, "转存")
                await asyncio.sleep(2)

            # 学习助手体验任务（30008）：发起一次 AI 对话
            if done.get("30008", True):
                self.tlog("AI任务已满, 跳过")
            else:
                try:
                    await self.campus_ai_chat()
                except Exception as e:
                    self.tlog_exc(e, "AI对话")

            lt = await self.req(
                "GET",
                "https://panservice.mail.wo.cn/activity/lottery/lottery-times"
                f"?activityId={self.CAMPUS_ACTIVITY_ID}",
                headers=self.campus_headers(),
            )
            times = (lt.get("data") or {}).get("result")
            times = times if isinstance(times, int) else 0
            if times <= 0:
                self.tlog("当前无可抽奖次数")
                return
            for _ in range(min(times, 5)):  # 上限保护: 防服务端异常次数导致长时间循环
                res = await self.campus_signed_post(
                    "/activity/lottery", "activity:lottery"
                )
                if str((res.get("meta") or {}).get("code")) == "200":
                    prize = (res.get("result") or {}).get("prizeName") or "未知奖品"
                    self.tlog(f"抽奖成功: {prize}")
                else:
                    self.tlog(f"抽奖失败 {response_summary(res)}")
                    break
                await asyncio.sleep(1.5)
        except Exception as e:
            self.tlog(f"异常: {self.format_exc(e)}")
