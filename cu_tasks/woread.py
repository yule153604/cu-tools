"""沃阅读与爱听积分。"""

import asyncio
import base64
import hashlib
import hmac
import random
import re
import string
import time
from datetime import datetime

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from cu_tasks.common import (
    api_response_err,
    iter_set_cookies,
)


class WoreadTasks:
    """活动实现，复用宿主的账号会话、请求与日志接口。"""

    async def woread_task(self):
        """沃阅读积分: ecs_token → accountLogin 换凭证 → 换票 → 刷时长 → 领积分
        链路: chinaUnicomCookie → onLine(ecs_token) → /account/login(手机号) → token/verifyCode
              → getPointCenterTicket → well ticket → addReadTime 刷时长 → jftask toFinish
        """
        self._task_tag = "沃阅读积分"
        self.tlog("开始")
        import json as _json

        BASE = "https://10010.woread.com.cn/ng_woread_service/rest"
        KEY = b"woreadst^&*12345"
        IV = b"16-Bytes--String"
        APPID = "10000002"
        APPSECRET = "7k1HcDL8RKvc"

        def get_aes(data: dict) -> str:
            """getAes: AES-CBC(key,iv) 加密 JSON → hex → base64"""
            plain = _json.dumps(data, separators=(",", ":"))
            c = AES.new(KEY, AES.MODE_CBC, iv=IV)
            ct = c.encrypt(pad(plain.encode(), 16))
            return base64.b64encode(ct.hex().encode()).decode()

        def get_aes2(phone: str) -> str:
            """getAes2: 同 AES-CBC 加密手机号"""
            c = AES.new(KEY, AES.MODE_CBC, iv=IV)
            ct = c.encrypt(pad(phone.encode(), 16))
            return base64.b64encode(ct.hex().encode()).decode()

        def ts_now() -> str:
            return datetime.now().strftime("%Y%m%d%H%M%S")

        # woread H5 请求头 (带 App 登录态 cookie)
        hdrs = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko)  unicom{version:iphone_c@12.1400}",
            "Content-Type": "application/json;charset=UTF-8",
            "Referer": "https://10010.woread.com.cn/ng_woread/",
            "Origin": "https://10010.woread.com.cn",
        }
        # 种 App 登录态 cookie (ecs_token/t3_token 等来自 self.client)
        self.client.cookies.set("ecs_token", self.ecs_token, domain=".woread.com.cn")
        self.client.cookies.set("ecs_token", self.ecs_token, domain=".10010.com.cn")
        self.client.cookies.set("u_account", self.mobile, domain=".woread.com.cn")

        async def woread_post(path: str, data: dict, **kw):
            return await self.req(
                "POST", BASE + path, headers=hdrs, json={"sign": get_aes(data)}, **kw
            )

        try:
            # ① getAccessToken (无需登录态)
            ts_ms = str(int(time.time() * 1000))
            md5sig = hashlib.md5(f"{APPID}{APPSECRET}{ts_ms}".encode()).hexdigest()
            r = await self.req(
                "POST",
                f"{BASE}/app/auth/{APPID}/{ts_ms}/{md5sig}",
                headers=hdrs,
                json={"sign": get_aes({"timestamp": ts_now()})},
            )
            accesstoken = ""
            if (res := r["data"]) and res.get("code") == "0000":
                accesstoken = res.get("data", {}).get("accesstoken", "")
            if not accesstoken:
                self.tlog(f"getAccessToken 失败: {api_response_err(r, 'app/auth')}")
                return
            hdrs["accesstoken"] = accesstoken

            # ② accountLogin: 手机号 AES2 加密 → token/verifyCode
            payload = {"phone": get_aes2(self.mobile), "timestamp": ts_now()}
            r = await self.req(
                "POST",
                BASE + "/account/login",
                headers={**hdrs, "noPassToken": "true"},
                json={"sign": get_aes(payload)},
            )
            if not (res := r["data"]) or res.get("code") != "0000":
                self.tlog(f"accountLogin 失败: {api_response_err(r, 'account/login')}")
                return
            d = res.get("data", {})
            token = d.get("token", "")
            verify_code = d.get("verifycode", "")
            user_id = d.get("userid", "")
            user_index = d.get("userindex", "")
            if not token or not verify_code:
                missing = ",".join(k for k in ("token", "verifycode") if not d.get(k))
                self.tlog(f"accountLogin 凭证不完整: 缺少 {missing}")
                return
            self.tlog(f"登录成功: {self.mobile[:3]}****{self.mobile[-4:]} 凭证已换")
            user_fields = {
                "token": token,
                "userId": user_id,
                "userIndex": user_index,
                "userAccount": self.mobile,
                "verifyCode": verify_code,
            }

            # ③ 换票: getPointCenterTicket → well URL + ticket
            r = await woread_post(
                "/activity/getPointCenterTicket", {"timestamp": ts_now(), **user_fields}
            )
            well_url = ""
            if (res := r["data"]) and res.get("code") == "0000":
                well_url = res.get("data", "") or ""
            if not well_url or "ticket=" not in well_url:
                self.tlog(f"换票失败: {api_response_err(r, 'getPointCenterTicket')}")
                return
            ticket = re.search(r"ticket=([^&]+)", well_url).group(1)
            self.tlog("换票成功")  # ticket 不入日志

            # ④ getSecretKey
            WELL_UA = hdrs["User-Agent"]
            r = await self.req(
                "GET",
                "https://m.jf.10010.com/jf-external-application/jftask/getSecretKey",
                headers={
                    "ticket": ticket,
                    "partnersid": "1706",
                    "clienttype": "aiting_unicom",
                    "User-Agent": WELL_UA,
                    "Referer": well_url,
                    "Accept": "application/json, text/plain, */*",
                },
            )
            if not (res := r["data"]) or str(res.get("code")) != "0000":
                self.tlog(f"getSecretKey 失败: {api_response_err(r, 'getSecretKey')}")
                return
            secret_key = res.get("data", {}).get("secretKey", "")
            if not secret_key:
                self.tlog("getSecretKey 无密钥")
                return
            jea = ""
            for ck in iter_set_cookies(r["headers"]):
                if "_jea_id=" in ck:
                    jea = ck.split("_jea_id=")[1].split(";")[0]
                    break

            def jf_headers():
                ts, nonce = (
                    str(int(time.time() * 1000)),
                    "".join(
                        random.choices(string.ascii_lowercase + string.digits, k=8)
                    ),
                )
                sig = hmac.new(
                    secret_key.encode(), f"{nonce}{ts}".encode(), hashlib.sha256
                ).hexdigest()
                return {
                    "ticket": ticket,
                    "User-Agent": WELL_UA,
                    "partnersid": "1706",
                    "clienttype": "aiting_unicom",
                    "Cookie": f"_jea_id={jea}",
                    "X-Request-Timestamp": ts,
                    "X-Request-Nonce": nonce,
                    "X-Request-Signature": sig,
                    "Referer": well_url,
                    "Origin": "https://m.jf.10010.com",
                    "Content-Type": "application/json;charset=UTF-8",
                    "Accept": "application/json, text/plain, */*",
                }

            # ⑤ 阅读刷时长: readChapter → newreadadd → addReadTime 三步完整复刻
            #    (新账号必须先读章节建立阅读记录, 每次运行报1次+2分钟, 服务端约10-15分钟冷却
            #     0/7/10点三次运行累计 → 读听看4分钟任务 2/2 完成)
            chapter_params = {
                "chapterSeno": "3",
                "cntIndex": 3120941,
                "beginChapter": 4,
                "timestamp": ts_now(),
                **user_fields,
            }
            # 必须 POST (HAR 实测: GET 报"请求方式不正确", 阅读会话不建立,
            # addReadTime 虽返回0000但任务不计时 → toFinish 假成功)
            r = await self.req(
                "POST",
                f"{BASE}/cnt/readChapter?cntindex=3120941&chapterallindex=120470473&chapterseno=3",
                headers=hdrs,
                json={"sign": get_aes(chapter_params)},
            )
            if (res := r["data"]) and res.get("code") == "0000":
                self.tlog(f"readChapter 读章节成功 (章节数={len(res.get('list', []))})")
            else:
                self.tlog(f"readChapter: {api_response_err(r, 'readChapter')}")
            await asyncio.sleep(1)

            newreadadd_data = {
                "userid": user_fields["userId"],
                "cntindex": 3120941,
                "chapterallindex": 120470473,
                "cnttype": 1,
                "cntname": "大清权臣李鸿章",
                "chaptertitle": "少年，胸怀壮志",
                "readtype": 1,
                "timestamp": ts_now(),
                **user_fields,
            }
            r = await self.req(
                "POST",
                BASE + "/basics/newreadadd",
                headers=hdrs,
                json={"sign": get_aes(newreadadd_data)},
            )
            if (res := r["data"]) and res.get("code") == "0000":
                self.tlog("newreadadd 更新阅读进度成功")
            await asyncio.sleep(1)

            read_data = {
                "readTime": 2,
                "cntIndex": 3120941,
                "cntType": 1,
                "cntindex": 3120941,
                "cnttype": 1,
                "chapterallindex": 120470473,
                "chapterseno": 3,
                "channelid": "18000688",
                "chapterid": "13120941003",
                "readtype": 1,
                "isend": "0",
                "timestamp": ts_now(),
                **user_fields,
            }
            r = await self.req(
                "POST",
                BASE + "/history/addReadTime",
                headers=hdrs,
                json={"sign": get_aes(read_data)},
            )
            if (res := r["data"]) and res.get("code") == "0000":
                self.tlog(
                    f"阅读上报: +2分钟 (weektime={res.get('data', {}).get('weektime')})"
                )
            else:
                self.tlog(
                    f"阅读上报: {api_response_err(r, 'addReadTime')} (冷却中/已满, 0点下次再报)"
                )

            # ⑥ 签到 (爱看/爱听签到, 每日+1)
            r = await self.req(
                "POST",
                "https://m.jf.10010.com/jf-external-application/uasptask/sign",
                headers=jf_headers(),
                json={"taskCode": "s818795876839563264"},
            )
            if (res := r["data"]) and res.get("code") == "0000":
                self.tlog(
                    f"签到成功: {res.get('data', {}).get('title', '')} (+{res.get('data', {}).get('score', '')})"
                )
            else:
                self.tlog(f"签到: {api_response_err(r, 'sign')} (可能已签)")

            # ⑥ taskDetail + toFinish 领积分
            r = await self.req(
                "POST",
                "https://m.jf.10010.com/jf-external-application/jftask/taskDetail",
                headers=jf_headers(),
                json={},
            )
            if not (res := r["data"]) or res.get("code") != "0000":
                self.tlog(f"taskDetail 失败: {api_response_err(r, 'taskDetail')}")
                return
            tasks = res.get("data", {}).get("taskDetail", {}).get("taskList", [])
            # 已实测(2026-08-07 taskRecords 验证):
            #   ✅ 真到账: 登录并读听看4分钟(读听看/新手) + 签到 → 保留
            #   ❌ 假成功: 分享爱听内容 toFinish 返回 true 但积分不到账
            #      (需真实微信分享回调, API 直调无效) → 跳过
            #   ❌ 不可行: 邀请(需真新用户) / 会员(需付费) / 限时福利(需爱看App) → 跳过
            skip_kw = ("邀请", "会员", "分享", "限时福利")
            claimed = 0
            for t in tasks:
                name = t.get("taskName", "")
                if any(k in name for k in skip_kw):
                    continue
                if t.get("finishCount", 0) >= t.get("needCount", 1):
                    continue
                r2 = await self.req(
                    "POST",
                    "https://m.jf.10010.com/jf-external-application/jftask/toFinish",
                    headers=jf_headers(),
                    json={"taskCode": t.get("taskCode")},
                )
                ok = (res2 := r2["data"]) and res2.get("data") is True
                self.tlog(f"领积分 [{name}]: {'成功' if ok else '未达条件'}")
                if ok:
                    claimed += 1
                await asyncio.sleep(1)
            # 查询今日积分
            r3 = await self.req(
                "POST",
                "https://m.jf.10010.com/jf-external-application/jftask/userInfo",
                headers=jf_headers(),
                json={},
            )
            if (res3 := r3["data"]) and res3.get("code") == "0000":
                ui = res3.get("data", {})
                self.tlog(
                    f"今日积分: {ui.get('todayEarnScore')} 可用: {ui.get('availableScore')} 累计: {ui.get('allEarnScore')}"
                )
            self.tlog(f"结束: 共领 {claimed} 项积分")
        except Exception as e:
            self.tlog_exc(e, "沃阅读积分")
