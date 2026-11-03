"""云盘登录、签名与上传共用能力。"""

import base64
import hashlib
import hmac
import json
import random
import time
from datetime import datetime

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from cu_tasks.common import (
    BATTLE_UA,
    CLOUD_BATTLE_FILEINFO_IV,
    CLOUD_PAN_SIGN_SECRET,
    api_response_err,
    response_summary,
)


class CloudTasks:
    """活动实现，复用宿主的账号会话、请求与日志接口。"""

    async def cloudpan_login(self):
        """云盘登录获取userToken"""
        try:
            if not self.ecs_token:
                return

            # 获取ticket
            r = await self.req(
                "GET",
                "https://m.client.10010.com/edop_ng/getTicketByNative",
                params={"appId": "edop_unicom_d67b3e30", "token": self.ecs_token},
            )
            ticket = r["data"].get("ticket") if isinstance(r["data"], dict) else None
            if not ticket:
                return

            # 获取userToken
            timestamp = str(int(time.time() * 1000))
            rnd = str(random.randint(123456, 199999))
            sign = hashlib.md5(
                f"HandheldHallAutoLoginV2{timestamp}{rnd}wohome".encode()
            ).hexdigest()

            r = await self.req(
                "POST",
                "https://panservice.mail.wo.cn/wohome/dispatcher",
                headers={
                    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; leijun Pro Build/SKQ1.22013.001)"
                },
                json={
                    "header": {
                        "key": "HandheldHallAutoLoginV2",
                        "resTime": timestamp,
                        "reqSeq": rnd,
                        "channel": "wohome",
                        "version": "",
                        "sign": sign,
                    },
                    "body": {"clientId": "1001000003", "ticket": ticket},
                },
            )

            if isinstance(r["data"], dict):
                # 尝试两种响应格式
                token = r["data"].get("body", {}).get("token", "") or r["data"].get(
                    "RSP", {}
                ).get("DATA", {}).get("token", "")
                if token:
                    self.cloud_disk = {"userToken": token}
                else:
                    self.log(f"云盘登录失败: {api_response_err(r, '获取userToken')}")
        except Exception as e:
            self.log_exc(e, "云盘登录")

    def cloud_disk_token(self):
        """联通云盘 userToken（上传大比拼等活动共用）。"""
        return (getattr(self, "cloud_disk", None) or {}).get("userToken", "")

    def cloud_activity_sign(self, payload):
        """panservice 活动请求签名。"""
        raw = (
            "&".join(f"{k}={payload[k]}" for k in sorted(payload))
            + f"&secret={CLOUD_PAN_SIGN_SECRET}"
        )
        return hmac.new(
            CLOUD_PAN_SIGN_SECRET.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()

    # --- 校园季/上传大比拼共用: 文件信息加密 ---

    def battle_encrypt_fileinfo(self, info, token):
        key = token[:16].encode()
        iv = CLOUD_BATTLE_FILEINFO_IV.encode()
        plaintext = json.dumps(info, separators=(",", ":"))
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return base64.b64encode(
            cipher.encrypt(pad(plaintext.encode(), AES.block_size))
        ).decode()

    async def cloud_upload2c(self, url, file_name, content, referer=None):
        """云盘 openapi upload2C 直传（校园季/上传大比拼共用）。返回 (ok, 描述)。"""
        token = self.cloud_disk_token()
        if not token:
            return False, "缺少云盘 userToken"
        file_info = {
            "batchNo": datetime.now().strftime("%Y%m%d%H%M%S"),
            "fileName": file_name,
            "fileSize": len(content),
            "fileType": 1,
            "directoryId": "0",
            "spaceType": "0",
        }
        form = {
            "uniqueId": f"{int(time.time() * 1000)}_{random.randint(100000, 999999)}",
            "accessToken": token,
            "psToken": "",
            "totalPart": "1",
            "partSize": str(len(content)),
            "partIndex": "1",
            "channel": "wocloud",
            "fileName": file_name,
            "fileSize": str(len(content)),
            "directoryId": "0",
            "spaceType": "0",
            "fileInfo": self.battle_encrypt_fileinfo(file_info, token),
        }
        headers = {
            "User-Agent": BATTLE_UA,
            "Referer": referer or self.battle_referer(),
            "accessToken": token,
            "access-token": token,
        }
        try:
            r = await self.client.post(
                url,
                data=form,
                files={"file": (file_name, content, "text/plain")},
                headers=headers,
                timeout=60.0,
            )
            try:
                d = r.json()
            except ValueError:
                d = {}
            if not r.is_success:
                return (
                    False,
                    f"HTTP {r.status_code}: {response_summary(d or r.text)}（上传结果待核实）",
                )
            if not isinstance(d, dict):
                return False, "上传响应格式错误（结果待核实）"
            if str(d.get("code")) == "0000":
                raw = d.get("data")
                fid = raw.get("fid", "") if isinstance(raw, dict) else ""
                return (
                    True,
                    f"上传成功 {file_name} ({len(content)}B) fid={str(fid)[:24]}...",
                )
            return False, response_summary(d or {"text": r.text[:120]})
        except httpx.RequestError as e:
            return False, f"{type(e).__name__}: 上传结果待核实"
