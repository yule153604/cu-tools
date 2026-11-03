"""通通农场（通通乡村）。"""

import asyncio
import random
import re
from datetime import datetime
from urllib.parse import quote

from cu_tasks.common import (
    TTXC_APP_BASE_URL,
    TTXC_BASE_URL,
    TTXC_CHANNEL,
    TTXC_GARBAGE_WAIT_SECONDS,
    TTXC_GROW_MAX_CHARGE_PER_LAND,
    TTXC_HARVEST_WAIT_SECONDS,
    TTXC_NEWBIE_STEPS,
    TTXC_REFERER,
    TTXC_UA,
    require_dict_data,
    safe_int,
)


class FarmTasks:
    """活动实现，复用宿主的账号会话、请求与日志接口。"""

    # === 6. 通通农场（通通乡村）===
    def ttxc_headers(self, auth=True, ecs=False):
        headers = {
            "User-Agent": TTXC_UA,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://epay.10010.com",
            "Referer": TTXC_REFERER,
            "X-Requested-With": "com.sinovatech.unicom.ui",
        }
        if auth and self.ttxc_token:
            headers["Authorization"] = self.ttxc_token
        if ecs and self.ecs_token:
            self.client.cookies.set("ecs_token", self.ecs_token, domain=".10010.com")
        return headers

    async def ttxc_post(self, path, payload=None, auth=True, with_user=True, ecs=False):
        data = dict(payload or {})
        if with_user:
            data.setdefault("userId", self.ttxc_user_id or "")
        data.setdefault("channel", TTXC_CHANNEL)
        r = await self.req(
            "POST",
            f"{TTXC_BASE_URL}{path}",
            json=data,
            headers=self.ttxc_headers(auth=auth, ecs=ecs),
        )
        return require_dict_data(r, path)

    def ttxc_snapshot_cookies(self):
        return [(c.name, c.value, c.domain) for c in self.client.cookies.jar]

    def ttxc_restore_cookies(self, saved):
        for name, value, domain in saved:
            self.client.cookies.set(name, value, domain=domain or ".10010.com")

    async def ttxc_finish_woauth(self, login_url, saved_cookies=None):
        headers = {"Referer": "https://epay.10010.com/", "User-Agent": TTXC_UA}
        r = await self.req("GET", login_url, headers=headers)
        if r["code"] != 200:
            return False
        text = r["data"] if isinstance(r["data"], str) else ""
        match = re.search(r'var token = "([^"]+)"', text)
        if not match:
            return False
        next_url = (
            "https://epay.10010.com/woauth2/after-collected-device-digest"
            f"?deviceDigestTraceId=&deviceDigestTokenId=&token={quote(match.group(1))}&source=app_sjyyt"
        )
        referer = login_url
        for _ in range(6):
            r = await self.req(
                "GET",
                next_url,
                allow_redirects=False,
                headers={"Referer": referer, "User-Agent": TTXC_UA},
            )
            location = r["headers"].get("location") or r["headers"].get("Location")
            if not location:
                ok = r["code"] == 200
                if ok and saved_cookies:
                    self.ttxc_restore_cookies(saved_cookies)
                    if self.ecs_token:
                        self.client.cookies.set(
                            "ecs_token", self.ecs_token, domain=".10010.com"
                        )
                return ok
            referer, next_url = next_url, location
        return False

    async def ttxc_init_ttgame(self):
        if not self.ecs_token:
            return False
        self.client.cookies.set("ecs_token", self.ecs_token, domain=".10010.com")
        url = f"{TTXC_APP_BASE_URL}/v1/login/ttGame?channel={TTXC_CHANNEL}&rptId="
        last = {}
        for attempt in range(1, 4):
            if self.ecs_token:
                self.client.cookies.set(
                    "ecs_token", self.ecs_token, domain=".10010.com"
                )
            r = await self.req(
                "POST",
                url,
                json={"unicomTokenId": self.unicom_token_id},
                headers=self.ttxc_headers(auth=False, ecs=True),
            )
            last = r["data"] if isinstance(r["data"], dict) else {}
            if last.get("code") == "0000":
                return True
            if last.get("code") == "4003" and last.get("data"):
                saved = self.ttxc_snapshot_cookies()
                if await self.ttxc_finish_woauth(last.get("data"), saved):
                    r = await self.req(
                        "POST",
                        url,
                        json={"unicomTokenId": self.unicom_token_id},
                        headers=self.ttxc_headers(auth=False, ecs=True),
                    )
                    last = r["data"] if isinstance(r["data"], dict) else {}
                    if last.get("code") == "0000":
                        return True
            if attempt < 3:
                await asyncio.sleep(2)
        self.tlog(f"初始化失败[{last.get('code')}]: {last.get('msg', '')}")
        return False

    async def ttxc_login(self, update_nick=True):
        if not self.ecs_token:
            self.tlog("缺少 ecs_token，跳过")
            return False
        if not await self.ttxc_init_ttgame():
            return False
        data = await self.ttxc_post(
            "/user/v1/login", auth=False, with_user=False, ecs=True
        )
        if data.get("code") != 0:
            self.tlog(f"登录失败[{data.get('code')}]: {data.get('msg', '')}")
            return False
        user = data.get("data") or {}
        self.ttxc_user_id = user.get("userId", "")
        self.ttxc_token = data.get("token", "")
        self.ttxc_charge_level = user.get("chargeLevel") or {}
        self.ttxc_newbie_list = user.get("newbieList")
        self.ttxc_nick_name = user.get("nickName") or ""
        if not self.ttxc_user_id or not self.ttxc_token:
            self.tlog("登录响应缺少 userId/token")
            return False
        carbon = self.ttxc_charge_level.get("carbonNum", 0)
        eco = self.ttxc_charge_level.get("ecologyAmount", 0)
        self.tlog(f"登录成功，碳能量{carbon}g，生态值{eco}")
        if update_nick and not self.ttxc_nick_name and self.ttxc_newbie_done():
            await self.ttxc_update_nick()
        return True

    async def ttxc_update_nick(self):
        nick = (self.mobile or "")[-4:] or str(random.randint(1000, 9999))
        data = await self.ttxc_post("/user/v1/updateNick", {"nickName": nick})
        if data.get("code") == 0:
            self.ttxc_nick_name = nick
            self.tlog(f"已设置昵称 {nick}")
            return True
        self.tlog(f"设置昵称失败[{data.get('code')}]: {data.get('msg', '')}")
        return False

    def ttxc_newbie_done(self):
        steps = getattr(self, "ttxc_newbie_list", None)
        if not isinstance(steps, list):
            # 接口改版/畸形响应: 维持跳过新手任务的现行为, 但留痕便于排查
            self.tlog(f"新手任务列表异常({type(steps).__name__}), 按已完成处理")
            return True
        return all(step in steps for step in TTXC_NEWBIE_STEPS)

    async def ttxc_newbie_mark(self, step):
        target = []
        for item in TTXC_NEWBIE_STEPS:
            target.append(item)
            if item == step:
                break
        data = await self.ttxc_post(
            "/user/v1/newbie", {"newbieList": target, "type": 1}
        )
        if data.get("code") == 0:
            self.ttxc_newbie_list = data.get("data") or target
            return True
        self.tlog(f"新手步骤{step}失败[{data.get('code')}]: {data.get('msg', '')}")
        return False

    def ttxc_newbie_need(self, step):
        steps = getattr(self, "ttxc_newbie_list", None)
        return isinstance(steps, list) and step not in steps

    async def ttxc_first_newbie_land(self, lands=None):
        lands = lands if lands is not None else await self.ttxc_get_lands()
        active = next(
            (
                land
                for land in lands
                if land.get("status") in [2, 3]
                and (land.get("plant") or {}).get("plantId")
            ),
            None,
        )
        if active:
            return active
        return next((land for land in lands if land.get("status") == 1), None)

    async def ttxc_newbie_charge_land(self):
        lands = await self.ttxc_get_lands()
        active = [
            land
            for land in lands
            if land.get("status") == 3 and (land.get("plant") or {}).get("plantId")
        ]
        return next(
            (
                land
                for land in active
                if str((land.get("plant") or {}).get("curLevel")) in ["0", "1"]
            ),
            None,
        ) or (active[0] if active else None)

    async def ttxc_harvest_land(self, land, newbie=False):
        if not land:
            return None
        plant = land.get("plant") or {}
        plant_id, land_index = plant.get("plantId"), land.get("landIndex")
        if not plant_id or not land_index:
            return None
        if land.get("status") == 2 and TTXC_HARVEST_WAIT_SECONDS > 0:
            await asyncio.sleep(TTXC_HARVEST_WAIT_SECONDS)
        path = "/plant/v1/newHarvest" if newbie else "/plant/v1/harvest"
        data = await self.ttxc_post(
            path, {"landIndex": land_index, "plantId": plant_id}
        )
        if data.get("code") == 0:
            self.tlog(f"地块{land_index}收获成功")
            return data.get("data") or {
                "landIndex": land_index,
                "status": 1,
                "plant": None,
            }
        self.tlog(
            f"地块{land_index}收获失败[{data.get('code')}]: {data.get('msg', '')}"
        )
        return None

    async def ttxc_newbie_task(self):
        if self.ttxc_newbie_done():
            return False
        need_farm = any(
            self.ttxc_newbie_need(step)
            for step in ["G03", "G03_2", "G04", "G05", "G09", "G10"]
        )
        lands = await self.ttxc_get_lands() if need_farm else []
        plant_id, current = "", None
        if need_farm:
            current = await self.ttxc_first_newbie_land(lands)
            await self.ttxc_post("/client/v1/plant/type", {})
            plant_id = await self.ttxc_get_plant_id()
            if not plant_id:
                self.tlog("新手任务缺少作物ID")
                return False
        if self.ttxc_newbie_need("G03") and not await self.ttxc_newbie_mark("G03"):
            return False
        if self.ttxc_newbie_need("G03_2"):
            has_crop = (
                current
                and current.get("status") in [2, 3]
                and (current.get("plant") or {}).get("plantId")
            )
            if not has_crop:
                data = await self.ttxc_post(
                    "/client/v1/plant/buy", {"plantId": plant_id, "gameCfgId": ""}
                )
                if data.get("code") != 0:
                    self.tlog(
                        f"新手购买作物失败[{data.get('code')}]: {data.get('msg', '')}"
                    )
                    return False
            if not await self.ttxc_newbie_mark("G03_2"):
                return False
        if self.ttxc_newbie_need("G04"):
            if not current or not current.get("landIndex"):
                self.tlog("新手任务缺少可种植地块")
                return False
            if not (
                current.get("status") in [2, 3]
                and (current.get("plant") or {}).get("plantId")
            ):
                data = await self.ttxc_post(
                    "/plant/v1/planting",
                    {"landIndex": current.get("landIndex"), "plantId": plant_id},
                )
                if data.get("code") != 0:
                    self.tlog(
                        f"新手种植失败[{data.get('code')}]: {data.get('msg', '')}"
                    )
                    return False
                current = data.get("data") or {
                    "landIndex": current.get("landIndex"),
                    "status": 3,
                    "plant": {"plantId": plant_id},
                }
            if not await self.ttxc_newbie_mark("G04"):
                return False
        if self.ttxc_newbie_need("G05"):
            current = (
                current
                if current and current.get("plant")
                else await self.ttxc_first_newbie_land()
            )
            current = await self.ttxc_charge_land(current)
            if not current or not await self.ttxc_newbie_mark("G05"):
                return False
        if self.ttxc_newbie_need("G09"):
            plant = (current or {}).get("plant") or {}
            level = str(plant.get("curLevel") or "")
            if not (
                current
                and current.get("status") == 3
                and plant.get("plantId")
                and level in ["", "0", "1"]
            ):
                current = await self.ttxc_newbie_charge_land()
            current = await self.ttxc_charge_land(current, mock=1)
            if not current or not await self.ttxc_newbie_mark("G09"):
                return False
        if self.ttxc_newbie_need("G10"):
            current = (
                current
                if current and current.get("plant")
                else await self.ttxc_first_newbie_land()
            )
            if not await self.ttxc_harvest_land(
                current, newbie=True
            ) or not await self.ttxc_newbie_mark("G10"):
                return False
        if self.ttxc_newbie_need("G11") and not await self.ttxc_newbie_mark("G11"):
            return False
        if self.ttxc_newbie_need("G12"):
            if not self.ttxc_nick_name and not await self.ttxc_update_nick():
                return False
            if not await self.ttxc_newbie_mark("G12"):
                return False
        self.tlog("新手任务已完成")
        return True

    async def ttxc_sign(self):
        info = await self.ttxc_post("/client/v1/sign/info", {})
        code = (info.get("data") or {}).get("signinCode")
        if not code:
            self.tlog("获取签到码失败")
            return
        user = await self.ttxc_post("/client/v1/sign/user", {"code": code})
        last_time = str((user.get("data") or {}).get("lastSigninTime") or "")
        if last_time[:10] == datetime.now().strftime("%Y-%m-%d"):
            self.tlog("今日已签到")
            return
        data = await self.ttxc_post("/client/v1/sign/signIn", {"code": code})
        if data.get("code") == 0:
            sign_data = data.get("data") or {}
            keep_value = safe_int(
                sign_data.get("keepSigninValue")
                or sign_data.get("lastKeepSigninValue")
                or sign_data.get("totalSigninValue")
            )
            award_items = (info.get("data") or {}).get("awards") or []
            energy = 0
            for item in award_items:
                if (
                    item.get("awardType") == "KEEP"
                    and safe_int(item.get("signinValue")) == keep_value
                ):
                    energy = safe_int(item.get("carbonEnergyAmount"))
                    break
            if not energy:
                charge_level = data.get("chargeLevel") or {}
                before = safe_int((self.ttxc_charge_level or {}).get("carbonNum"))
                after = safe_int(charge_level.get("carbonNum"))
                energy = max(after - before, 0)
            if data.get("chargeLevel"):
                self.ttxc_charge_level = (
                    data.get("chargeLevel") or self.ttxc_charge_level
                )
            self.tlog(f"签到成功 +{energy}g" if energy else "签到成功")
        else:
            self.tlog(f"签到失败[{data.get('code')}]: {data.get('msg', '')}")

    async def ttxc_get_tasks(self):
        data = await self.ttxc_post("/client/v1/task/list", {})
        if data.get("code") != 0:
            self.tlog(f"获取任务列表失败[{data.get('code')}]: {data.get('msg', '')}")
            return []
        tasks = []
        for group in data.get("data") or []:
            for task in group.get("taskList") or []:
                task["taskGroupName"] = group.get("taskGroupName", "")
                tasks.append(task)
        return tasks

    async def ttxc_finish_task(self, task):
        task_id = task.get("taskCode")
        if not task_id:
            return False
        data = await self.ttxc_post("/client/v1/task/finish", {"taskId": task_id})
        name = task.get("taskTitle", task_id)
        if data.get("code") == 0:
            reward = task.get("carbonEnergyAmount") or 0
            self.tlog(f"领取[{name}]成功 +{reward}g")
            return True
        self.tlog(f"领取[{name}]失败[{data.get('code')}]: {data.get('msg', '')}")
        return False

    async def ttxc_do_task(self, task):
        data = await self.ttxc_post(
            "/client/v1/task/do", {"taskId": task.get("taskCode")}
        )
        name = task.get("taskTitle", task.get("taskCode", ""))
        if data.get("code") == 0:
            self.tlog(f"已执行[{name}]")
            return True
        self.tlog(f"执行[{name}]失败[{data.get('code')}]: {data.get('msg', '')}")
        return False

    async def ttxc_claim_ready_tasks(self, tasks, claimed=None):
        if claimed is None:
            claimed = set()
        count = 0
        for task in tasks:
            task_id = task.get("taskCode")
            if task.get("taskStatus") == "UNCLA" and task_id not in claimed:
                if await self.ttxc_finish_task(task):
                    claimed.add(task_id)
                    count += 1
        return count

    async def ttxc_do_jump_tasks(self, tasks):
        count = 0
        for task in tasks:
            if (
                task.get("taskType") == "GAME"
                and task.get("taskStatus") == "UNDO"
                and task.get("jumpUrl")
            ):
                if await self.ttxc_do_task(task):
                    count += 1
                await asyncio.sleep(1)
        return count

    async def ttxc_do_garbage_task(self, tasks):
        task = next(
            (
                t
                for t in tasks
                if t.get("taskType") == "GAME"
                and t.get("taskStatus") == "UNDO"
                and "垃圾分类" in t.get("taskTitle", "")
            ),
            None,
        )
        if not task:
            return False
        start = await self.ttxc_post("/user/v1/start", {})
        answer_no = (start.get("data") or {}).get("answerNo")
        if not answer_no:
            self.tlog("垃圾分类开始失败")
            return False
        await asyncio.sleep(TTXC_GARBAGE_WAIT_SECONDS)
        data = await self.ttxc_post("/user/v1/finish", {"answerNo": answer_no})
        if data.get("code") == 0:
            self.tlog("垃圾分类已通关")
            return True
        self.tlog(f"垃圾分类通关失败[{data.get('code')}]: {data.get('msg', '')}")
        return False

    async def ttxc_prepare_newbie_energy(self, claimed=None):
        if claimed is None:
            claimed = set()
        await self.ttxc_sign()
        tasks = await self.ttxc_get_tasks()
        await self.ttxc_claim_ready_tasks(tasks, claimed)
        await self.ttxc_do_jump_tasks(tasks)
        await self.ttxc_do_garbage_task(tasks)
        tasks = await self.ttxc_get_tasks()
        await self.ttxc_claim_ready_tasks(tasks, claimed)

    async def ttxc_get_lands(self):
        land = safe_int((self.ttxc_charge_level or {}).get("land"), 4)
        data = await self.ttxc_post("/plant/v1/user", {"land": land})
        if data.get("code") != 0:
            self.tlog(f"获取土地失败[{data.get('code')}]: {data.get('msg', '')}")
            return []
        return data.get("data") or []

    async def ttxc_get_plant_id(self):
        data = await self.ttxc_post(
            "/client/v1/plant/page", {"itemType": "SPE", "pageNum": 1, "pageSize": 20}
        )
        items = (data.get("data") or {}).get("list") or []
        return items[0].get("itemNo", "") if items else ""

    async def ttxc_plant_land(self, land_index, plant_id=None):
        plant_id = plant_id or await self.ttxc_get_plant_id()
        if not plant_id or not land_index:
            return None
        buy = await self.ttxc_post(
            "/client/v1/plant/buy", {"plantId": plant_id, "gameCfgId": ""}
        )
        if buy.get("code") != 0:
            self.tlog(f"购买种子失败[{buy.get('code')}]: {buy.get('msg', '')}")
            return None
        data = await self.ttxc_post(
            "/plant/v1/planting", {"landIndex": land_index, "plantId": plant_id}
        )
        if data.get("code") == 0:
            self.tlog(f"已在地块{land_index}种植作物")
            return {
                "landIndex": land_index,
                "status": 3,
                "plant": {"plantId": plant_id},
            }
        self.tlog(
            f"地块{land_index}种植失败[{data.get('code')}]: {data.get('msg', '')}"
        )
        return None

    async def ttxc_ensure_planted_lands(self, lands):
        active = [
            land
            for land in lands
            if land.get("status") in [2, 3] and (land.get("plant") or {}).get("plantId")
        ]
        empty = [land for land in lands if land.get("status") == 1]
        if not empty:
            return active
        plant_id = await self.ttxc_get_plant_id()
        if not plant_id:
            return active
        for land in empty:
            planted = await self.ttxc_plant_land(land.get("landIndex"), plant_id)
            if planted:
                active.append(planted)
        return active

    async def ttxc_charge_land(self, land, mock=None):
        if not land:
            return None
        plant = land.get("plant") or {}
        plant_id, land_index = plant.get("plantId"), land.get("landIndex")
        if not plant_id or not land_index:
            return None
        payload = {"landIndex": land_index, "plantId": plant_id}
        if mock is not None:
            payload["mock"] = mock
        data = await self.ttxc_post("/plant/v1/charge", payload)
        if data.get("code") == 0:
            self.tlog(f"地块{land_index}充能成功")
            result = data.get("data") or {}
            if result and not result.get("plant"):
                result["plant"] = plant
            return result or land
        self.tlog(
            f"地块{land_index}充能失败[{data.get('code')}]: {data.get('msg', '')}"
        )
        if "余额不足" in str(data.get("msg", "")):
            self.ttxc_no_energy = True
        return None

    async def ttxc_harvest_and_replant(self, land):
        harvested = await self.ttxc_harvest_land(land)
        if harvested and land:
            return await self.ttxc_plant_land(land.get("landIndex"))
        return None

    async def ttxc_grow_land_to_harvest(self, land):
        current = land
        if current.get("status") == 2:
            await self.ttxc_harvest_and_replant(current)
            return
        charged = 0
        while current.get("status") == 3 and charged < TTXC_GROW_MAX_CHARGE_PER_LAND:
            if self.ttxc_no_energy:
                return
            current = await self.ttxc_charge_land(current)
            if not current:
                return
            charged += 1
            if current.get("status") == 2:
                await self.ttxc_harvest_and_replant(current)
                return
            if charged < TTXC_GROW_MAX_CHARGE_PER_LAND:
                await asyncio.sleep(1)
        if current.get("status") == 3:
            self.tlog(f"地块{current.get('landIndex')}催熟达到上限，跳过")

    def ttxc_replace_land(self, lands, updated):
        land_index = updated.get("landIndex")
        for i, land in enumerate(lands):
            if land.get("landIndex") == land_index:
                lands[i] = updated
                return
        lands.append(updated)

    async def ttxc_complete_charge_task(self, active, remaining):
        while remaining > 0:
            if self.ttxc_no_energy:
                return
            immature = [
                land
                for land in active
                if land.get("status") == 3 and (land.get("plant") or {}).get("plantId")
            ]
            if not immature:
                self.tlog("未成熟作物不足，提前结束10次充能补足")
                return
            progressed = False
            for land in immature:
                if remaining <= 0 or self.ttxc_no_energy:
                    return
                result = await self.ttxc_charge_land(land)
                if result:
                    self.ttxc_replace_land(active, result)
                    remaining -= 1
                    progressed = True
                await asyncio.sleep(1)
            if not progressed:
                self.tlog("充能未成功，提前结束10次充能补足")
                return

    async def ttxc_farm_tasks(self, tasks):
        charge_task = next(
            (t for t in tasks if "10次作物充能" in t.get("taskTitle", "")), None
        )
        land_task = next(
            (t for t in tasks if "三块不同" in t.get("taskTitle", "")), None
        )
        harvest_task = next(
            (t for t in tasks if "收获一次作物" in t.get("taskTitle", "")), None
        )
        if not charge_task and not land_task and not harvest_task:
            return
        charge_pending = (
            charge_task if (charge_task or {}).get("taskStatus") == "UNDO" else None
        )
        land_pending = (
            land_task if (land_task or {}).get("taskStatus") == "UNDO" else None
        )
        harvest_pending = (
            harvest_task if (harvest_task or {}).get("taskStatus") == "UNDO" else None
        )
        if not charge_pending and not land_pending and not harvest_pending:
            return
        lands = await self.ttxc_get_lands()
        active = await self.ttxc_ensure_planted_lands(lands)
        need_land = max(
            safe_int((land_pending or {}).get("finishValue"))
            - safe_int((land_pending or {}).get("doneValue")),
            0,
        )
        if harvest_pending and not charge_pending and not land_pending:
            for land in active:
                if land.get("status") == 2:
                    await self.ttxc_harvest_and_replant(land)
        if not active:
            self.tlog("没有可充能作物")
            return
        charged = 0
        for i, land in enumerate(active[:need_land]):
            if self.ttxc_no_energy:
                break
            result = await self.ttxc_charge_land(land)
            if result:
                active[i] = result
                charged += 1
                await asyncio.sleep(1)
        need_charge = max(
            safe_int((charge_pending or {}).get("finishValue"))
            - safe_int((charge_pending or {}).get("doneValue"))
            - charged,
            0,
        )
        await self.ttxc_complete_charge_task(active, need_charge)
        for land in active:
            await self.ttxc_grow_land_to_harvest(land)

    async def farm_task(self):
        self._task_tag = "通通乡村"
        self.tlog("开始")
        if not await self.ttxc_login():
            return
        claimed = set()
        if not self.ttxc_newbie_done():
            await self.ttxc_prepare_newbie_energy(claimed)
        if not self.ttxc_newbie_done() and not await self.ttxc_newbie_task():
            return
        await self.ttxc_sign()
        tasks = await self.ttxc_get_tasks()
        await self.ttxc_claim_ready_tasks(tasks, claimed)
        await self.ttxc_do_jump_tasks(tasks)
        await self.ttxc_do_garbage_task(tasks)
        await self.ttxc_farm_tasks(tasks)
        tasks = await self.ttxc_get_tasks()
        await self.ttxc_claim_ready_tasks(tasks, claimed)
