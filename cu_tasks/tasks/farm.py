"""通通农场（通通乡村）。"""

import asyncio
import random
import re
from datetime import datetime
from urllib.parse import quote

from cu_tasks.base import Task
from cu_tasks.common import require_dict_data, safe_int

BASE_URL = "https://epay.10010.com/cu-ca-game-front"
APP_BASE_URL = "https://epay.10010.com/cu-ca-app-front"
CHANNEL = "225"
REFERER = "https://epay.10010.com/cu-ca-game-web/index.html?channel=qdqp"
UA = (
    "Mozilla/5.0 (Linux; Android 10; MI 8 Build/QKQ1.190828.002; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/143.0.7499.146 "
    "Mobile Safari/537.36; unicom{version:android@11.0802,desmobile:0};"
    "devicetype{deviceBrand:Xiaomi,deviceModel:MI 8}"
)
NEWBIE_STEPS = ("G01", "G02", "G03", "G03_2", "G04", "G05", "G09", "G10", "G11", "G12")
CHARGE_COST = 50
SEED_COST = 100
TRANSIENT_TASK_ERRORS = (
    "i/o error",
    "connection reset",
    "timed out",
    "timeout",
    "网络/请求异常",
    "http=50",
    "繁忙",
    "稍后",
)


class FarmTask(Task):
    """通通农场：新手引导、签到、任务、种植/充能/收获。"""

    name, label = "farm", "通通乡村"

    def __init__(self, account, settings=None):
        super().__init__(account, settings)
        self.token = self.nick_name = self.user_id = ""
        self.newbie_list = []
        self.charge_level = {}
        self.carbon: int | None = None
        self.no_energy = False
        self.no_buy_energy = False

    def headers(self, auth=True, ecs=False):
        if ecs:
            self.pin_ecs_cookie()
        headers = {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://epay.10010.com",
            "Referer": REFERER,
            "X-Requested-With": "com.sinovatech.unicom.ui",
        }
        if auth and self.token:
            headers["Authorization"] = self.token
        return headers

    def pin_ecs_cookie(self):
        """woauth 跳转可能覆盖 ecs_token，请求前重新钉住当前账号的值。"""
        if self.ecs_token:
            self.client.cookies.set("ecs_token", self.ecs_token, domain=".10010.com")

    async def post(self, path, payload=None, auth=True, with_user=True, ecs=False):
        data = dict(payload or {})
        if with_user:
            data.setdefault("userId", self.user_id or "")
        data.setdefault("channel", CHANNEL)
        r = await self.req(
            "POST",
            f"{BASE_URL}{path}",
            json=data,
            headers=self.headers(auth=auth, ecs=ecs),
        )
        return require_dict_data(r, path)

    def snapshot_cookies(self):
        return [(c.name, c.value, c.domain) for c in self.client.cookies.jar]

    def restore_cookies(self, saved):
        for name, value, domain in saved:
            self.client.cookies.set(name, value, domain=domain or ".10010.com")

    async def finish_woauth(self, login_url, saved_cookies=None):
        headers = {"Referer": "https://epay.10010.com/", "User-Agent": UA}
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
                headers={"Referer": referer, "User-Agent": UA},
            )
            location = r["headers"].get("location") or r["headers"].get("Location")
            if not location:
                ok = r["code"] == 200
                if ok and saved_cookies:
                    self.restore_cookies(saved_cookies)
                    self.pin_ecs_cookie()
                return ok
            referer, next_url = next_url, location
        return False

    async def init_ttgame(self):
        if not self.ecs_token:
            return False
        url = f"{APP_BASE_URL}/v1/login/ttGame?channel={CHANNEL}&rptId="

        async def tt_game():
            r = await self.req(
                "POST",
                url,
                json={"unicomTokenId": self.unicom_token_id},
                headers=self.headers(auth=False, ecs=True),
            )
            return r["data"] if isinstance(r["data"], dict) else {}

        last = {}
        for attempt in range(1, 4):
            last = await tt_game()
            if last.get("code") == "0000":
                return True
            if last.get("code") == "4003" and last.get("data"):
                saved = self.snapshot_cookies()
                if await self.finish_woauth(last.get("data"), saved):
                    last = await tt_game()
                    if last.get("code") == "0000":
                        return True
            if attempt < 3:
                await asyncio.sleep(2)
        self.tlog(f"初始化失败[{last.get('code')}]: {last.get('msg', '')}")
        return False

    async def login(self, update_nick=True):
        if not self.ecs_token:
            self.tlog("缺少 ecs_token，跳过")
            return False
        if not await self.init_ttgame():
            return False
        data = await self.post("/user/v1/login", auth=False, with_user=False, ecs=True)
        if data.get("code") != 0:
            self.tlog(f"登录失败[{data.get('code')}]: {data.get('msg', '')}")
            return False
        user = data.get("data") or {}
        self.user_id = user.get("userId", "")
        self.token = data.get("token", "")
        self.charge_level = user.get("chargeLevel") or {}
        self.newbie_list = user.get("newbieList")
        self.nick_name = user.get("nickName") or ""
        if not self.user_id or not self.token:
            self.tlog("登录响应缺少 userId/token")
            return False
        carbon = safe_int(self.charge_level.get("carbonNum"), 0)
        self.carbon = carbon
        eco = self.charge_level.get("ecologyAmount", 0)
        self.tlog(f"登录成功，碳能量{carbon}g，生态值{eco}")
        if update_nick and not self.nick_name and self.newbie_done():
            await self.update_nick()
        return True

    async def update_nick(self):
        nick = (self.mobile or "")[-4:] or str(random.randint(1000, 9999))
        data = await self.post("/user/v1/updateNick", {"nickName": nick})
        if data.get("code") == 0:
            self.nick_name = nick
            self.tlog(f"已设置昵称 {nick}")
            return True
        self.tlog(f"设置昵称失败[{data.get('code')}]: {data.get('msg', '')}")
        return False

    def newbie_done(self):
        steps = self.newbie_list
        if not isinstance(steps, list):
            # 接口改版/畸形响应: 维持跳过新手任务的现行为, 但留痕便于排查
            self.tlog(f"新手任务列表异常({type(steps).__name__}), 按已完成处理")
            return True
        return all(step in steps for step in NEWBIE_STEPS)

    async def newbie_mark(self, step):
        target = []
        for item in NEWBIE_STEPS:
            target.append(item)
            if item == step:
                break
        data = await self.post("/user/v1/newbie", {"newbieList": target, "type": 1})
        if data.get("code") == 0:
            self.newbie_list = data.get("data") or target
            return True
        self.tlog(f"新手步骤{step}失败[{data.get('code')}]: {data.get('msg', '')}")
        return False

    def newbie_need(self, step):
        steps = self.newbie_list
        return isinstance(steps, list) and step not in steps

    async def first_newbie_land(self, lands=None):
        lands = lands if lands is not None else await self.get_lands()
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

    async def newbie_charge_land(self):
        lands = await self.get_lands()
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

    async def harvest_land(self, land, newbie=False):
        if not land:
            return None
        plant = land.get("plant") or {}
        plant_id, land_index = plant.get("plantId"), land.get("landIndex")
        if not plant_id or not land_index:
            return None
        if land.get("status") == 2 and self.settings.farm_harvest_wait > 0:
            await asyncio.sleep(self.settings.farm_harvest_wait)
        path = "/plant/v1/newHarvest" if newbie else "/plant/v1/harvest"
        data = await self.post(path, {"landIndex": land_index, "plantId": plant_id})
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

    async def newbie_task(self):
        if self.newbie_done():
            return False
        need_farm = any(
            self.newbie_need(step)
            for step in ["G03", "G03_2", "G04", "G05", "G09", "G10"]
        )
        lands = await self.get_lands() if need_farm else []
        plant_id, current = "", None
        if need_farm:
            current = await self.first_newbie_land(lands)
            await self.post("/client/v1/plant/type", {})
            plant_id = await self.get_plant_id()
            if not plant_id:
                self.tlog("新手任务缺少作物ID")
                return False
        if self.newbie_need("G03") and not await self.newbie_mark("G03"):
            return False
        if self.newbie_need("G03_2"):
            has_crop = (
                current
                and current.get("status") in [2, 3]
                and (current.get("plant") or {}).get("plantId")
            )
            if not has_crop:
                data = await self.post(
                    "/client/v1/plant/buy", {"plantId": plant_id, "gameCfgId": ""}
                )
                if data.get("code") != 0:
                    self.tlog(
                        f"新手购买作物失败[{data.get('code')}]: {data.get('msg', '')}"
                    )
                    return False
            if not await self.newbie_mark("G03_2"):
                return False
        if self.newbie_need("G04"):
            if not current or not current.get("landIndex"):
                self.tlog("新手任务缺少可种植地块")
                return False
            if not (
                current.get("status") in [2, 3]
                and (current.get("plant") or {}).get("plantId")
            ):
                data = await self.post(
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
            if not await self.newbie_mark("G04"):
                return False
        if self.newbie_need("G05"):
            current = (
                current
                if current and current.get("plant")
                else await self.first_newbie_land()
            )
            current = await self.charge_land(current)
            if not current or not await self.newbie_mark("G05"):
                return False
        if self.newbie_need("G09"):
            plant = (current or {}).get("plant") or {}
            level = str(plant.get("curLevel") or "")
            if not (
                current
                and current.get("status") == 3
                and plant.get("plantId")
                and level in ["", "0", "1"]
            ):
                current = await self.newbie_charge_land()
            current = await self.charge_land(current, mock=1)
            if not current or not await self.newbie_mark("G09"):
                return False
        if self.newbie_need("G10"):
            current = (
                current
                if current and current.get("plant")
                else await self.first_newbie_land()
            )
            if not await self.harvest_land(
                current, newbie=True
            ) or not await self.newbie_mark("G10"):
                return False
        if self.newbie_need("G11") and not await self.newbie_mark("G11"):
            return False
        if self.newbie_need("G12"):
            if not self.nick_name and not await self.update_nick():
                return False
            if not await self.newbie_mark("G12"):
                return False
        self.tlog("新手任务已完成")
        return True

    @staticmethod
    def _is_transient_error(err) -> bool:
        lowered = str(err or "").lower()
        return any(k in lowered for k in TRANSIENT_TASK_ERRORS)

    async def _post_with_retry(self, path, payload, attempts=3):
        data = {}
        for attempt in range(attempts):
            try:
                data = await self.post(path, payload)
            except ValueError as exc:
                if attempt + 1 < attempts and self._is_transient_error(exc):
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise
            if data.get("code") == 0:
                return data
            if attempt + 1 < attempts and self._is_transient_error(data.get("msg", "")):
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            return data
        return data

    async def sign(self):
        info = await self._post_with_retry("/client/v1/sign/info", {})
        code = (info.get("data") or {}).get("signinCode")
        if info.get("code") != 0 or not code:
            self.tlog("获取签到码失败")
            return
        user = await self._post_with_retry("/client/v1/sign/user", {"code": code})
        if user.get("code") != 0 or not isinstance(user.get("data"), dict):
            self.tlog(f"查询签到状态失败[{user.get('code')}]: {user.get('msg', '')}")
            return
        last_time = str(user["data"].get("lastSigninTime") or "")
        if last_time[:10] == datetime.now().strftime("%Y-%m-%d"):
            self.tlog("今日已签到")
            return
        data = await self.post("/client/v1/sign/signIn", {"code": code})
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
                before = safe_int((self.charge_level or {}).get("carbonNum"))
                after = safe_int(charge_level.get("carbonNum"))
                energy = max(after - before, 0)
            if data.get("chargeLevel"):
                self.charge_level = data.get("chargeLevel") or self.charge_level
                if self.charge_level.get("carbonNum") is not None:
                    self.carbon = safe_int(self.charge_level.get("carbonNum"), 0)
            elif self.carbon is not None:
                self.carbon += energy
            if not energy and not sign_data:
                self.tlog("今日已签到")
            else:
                self.tlog(f"签到成功 +{energy}g" if energy else "签到成功")
        else:
            self.tlog(f"签到失败[{data.get('code')}]: {data.get('msg', '')}")

    async def get_tasks(self):
        data = await self._post_with_retry("/client/v1/task/list", {})
        if data.get("code") != 0:
            self.tlog(f"获取任务列表失败[{data.get('code')}]: {data.get('msg', '')}")
            return []
        tasks = []
        for group in data.get("data") or []:
            for task in group.get("taskList") or []:
                task["taskGroupName"] = group.get("taskGroupName", "")
                tasks.append(task)
        return tasks

    async def finish_task(self, task):
        task_id = task.get("taskCode")
        if not task_id:
            return False
        name = task.get("taskTitle", task_id)
        data = await self._post_with_retry(
            "/client/v1/task/finish", {"taskId": task_id}
        )
        if data.get("code") == 0:
            reward = safe_int(task.get("carbonEnergyAmount"), 0)
            if isinstance(data.get("chargeLevel"), dict) and (
                data["chargeLevel"].get("carbonNum") is not None
            ):
                self.carbon = safe_int(data["chargeLevel"].get("carbonNum"), 0)
            elif self.carbon is not None or reward > 0:
                self.carbon = (self.carbon or 0) + reward
            self.tlog(f"领取[{name}]成功 +{reward}g")
            return True
        self.tlog(f"领取[{name}]失败[{data.get('code')}]: {data.get('msg', '')}")
        return False

    async def do_task(self, task):
        code = task.get("taskCode")
        name = task.get("taskTitle", code or "")
        data = await self._post_with_retry("/client/v1/task/do", {"taskId": code})
        if data.get("code") == 0:
            self.tlog(f"已执行[{name}]")
            return True
        self.tlog(f"执行[{name}]失败[{data.get('code')}]: {data.get('msg', '')}")
        return False

    async def claim_ready_tasks(self, tasks, claimed=None):
        if claimed is None:
            claimed = set()
        count = 0
        for task in tasks:
            task_id = task.get("taskCode")
            if task.get("taskStatus") == "UNCLA" and task_id not in claimed:
                if await self.finish_task(task):
                    claimed.add(task_id)
                    count += 1
        return count

    async def do_jump_tasks(self, tasks):
        count = 0
        for task in tasks:
            if (
                task.get("taskType") == "GAME"
                and task.get("taskStatus") == "UNDO"
                and task.get("jumpUrl")
            ):
                if await self.do_task(task):
                    count += 1
                await asyncio.sleep(1)
        return count

    async def do_garbage_task(self, tasks):
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
        start = await self.post("/user/v1/start", {})
        answer_no = (start.get("data") or {}).get("answerNo")
        if not answer_no:
            self.tlog("垃圾分类开始失败")
            return False
        await asyncio.sleep(self.settings.farm_garbage_wait)
        data = await self.post("/user/v1/finish", {"answerNo": answer_no})
        if data.get("code") == 0:
            self.tlog("垃圾分类已通关")
            return True
        self.tlog(f"垃圾分类通关失败[{data.get('code')}]: {data.get('msg', '')}")
        return False

    async def prepare_newbie_energy(self, claimed=None):
        if claimed is None:
            claimed = set()
        await self.sign()
        tasks = await self.get_tasks()
        await self.claim_ready_tasks(tasks, claimed)
        await self.do_jump_tasks(tasks)
        await self.do_garbage_task(tasks)
        tasks = await self.get_tasks()
        await self.claim_ready_tasks(tasks, claimed)

    async def get_lands(self):
        land = safe_int((self.charge_level or {}).get("land"), 4)
        data = await self.post("/plant/v1/user", {"land": land})
        if data.get("code") != 0:
            self.tlog(f"获取土地失败[{data.get('code')}]: {data.get('msg', '')}")
            return []
        return data.get("data") or []

    async def get_plant_id(self):
        data = await self.post(
            "/client/v1/plant/page", {"itemType": "SPE", "pageNum": 1, "pageSize": 20}
        )
        items = (data.get("data") or {}).get("list") or []
        return items[0].get("itemNo", "") if items else ""

    def _can_buy_seed(self) -> bool:
        if self.no_energy or self.no_buy_energy:
            return False
        if self.carbon is not None and self.carbon < SEED_COST:
            if self.carbon < CHARGE_COST:
                self.no_energy = True
            self.no_buy_energy = True
            return False
        return True

    async def plant_land(self, land_index, plant_id=None):
        if not self._can_buy_seed():
            return None
        plant_id = plant_id or await self.get_plant_id()
        if not plant_id or not land_index:
            return None
        buy = await self.post(
            "/client/v1/plant/buy", {"plantId": plant_id, "gameCfgId": ""}
        )
        if buy.get("code") != 0:
            msg = str(buy.get("msg", ""))
            if any(k in msg for k in ("能量不足", "余额不足")):
                self.no_buy_energy = True
                if self.carbon is not None and self.carbon >= SEED_COST:
                    self.carbon = SEED_COST - 1
                self.tlog(f"地块{land_index}碳能量不足，暂不补种")
                return None
            self.tlog(f"购买种子失败[{buy.get('code')}]: {msg}")
            return None
        if isinstance(buy.get("chargeLevel"), dict) and (
            buy["chargeLevel"].get("carbonNum") is not None
        ):
            self.carbon = safe_int(buy["chargeLevel"].get("carbonNum"), 0)
        elif self.carbon is not None:
            self.carbon = max(self.carbon - SEED_COST, 0)
        data = await self.post(
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

    async def ensure_planted_lands(self, lands):
        active = [
            land
            for land in lands
            if land.get("status") in [2, 3] and (land.get("plant") or {}).get("plantId")
        ]
        empty = [land for land in lands if land.get("status") == 1]
        if not empty or not self._can_buy_seed():
            return active
        plant_id = await self.get_plant_id()
        if not plant_id:
            return active
        for land in empty:
            if not self._can_buy_seed():
                break
            planted = await self.plant_land(land.get("landIndex"), plant_id)
            if planted:
                active.append(planted)
        return active

    @staticmethod
    def _land_maturity_key(land) -> tuple[int, int]:
        plant = (land or {}).get("plant") or {}
        return (
            safe_int(plant.get("curLevel"), 0),
            safe_int(plant.get("curCarbonNum"), 0),
        )

    async def charge_land(self, land, mock=None):
        if not land or (mock is None and land.get("status") == 2):
            return None
        if mock is None and (
            self.no_energy or (self.carbon is not None and self.carbon < CHARGE_COST)
        ):
            self.no_energy = True
            return None
        plant = land.get("plant") or {}
        plant_id, land_index = plant.get("plantId"), land.get("landIndex")
        if not plant_id or not land_index:
            return None
        payload = {"landIndex": land_index, "plantId": plant_id}
        if mock is not None:
            payload["mock"] = mock
        data = await self.post("/plant/v1/charge", payload)
        if data.get("code") == 0:
            self.tlog(f"地块{land_index}充能成功")
            result = data.get("data") or {}
            if isinstance(data.get("chargeLevel"), dict) and (
                data["chargeLevel"].get("carbonNum") is not None
            ):
                self.carbon = safe_int(data["chargeLevel"].get("carbonNum"), 0)
            elif mock is None and self.carbon is not None:
                self.carbon = max(self.carbon - CHARGE_COST, 0)
            if mock is None and self.carbon is not None and self.carbon < CHARGE_COST:
                self.no_energy = True
            if result and not result.get("plant"):
                result["plant"] = plant
            return result or land
        msg = str(data.get("msg", ""))
        if any(k in msg for k in ("余额不足", "能量不足")):
            self.no_energy = True
            self.no_buy_energy = True
            if self.carbon is not None and self.carbon >= CHARGE_COST:
                self.carbon = CHARGE_COST - 1
            self.tlog(f"地块{land_index}碳能量不足，停止充能")
            return None
        self.tlog(f"地块{land_index}充能失败[{data.get('code')}]: {msg}")
        return None

    async def harvest_and_replant(self, land):
        harvested = await self.harvest_land(land)
        if harvested and land:
            return await self.plant_land(land.get("landIndex"))
        return None

    async def grow_land_to_harvest(self, land):
        current = land
        if current.get("status") == 2:
            await self.harvest_and_replant(current)
            return
        max_charge = self.settings.farm_max_charge_per_land
        charged = 0
        while current.get("status") == 3 and charged < max_charge:
            if self.no_energy:
                return
            current = await self.charge_land(current)
            if not current:
                return
            charged += 1
            if current.get("status") == 2:
                await self.harvest_and_replant(current)
                return
            if charged < max_charge:
                await asyncio.sleep(1)
        if current.get("status") == 3:
            self.tlog(f"地块{current.get('landIndex')}催熟达到上限，跳过")

    def replace_land(self, lands, updated):
        land_index = updated.get("landIndex")
        for i, land in enumerate(lands):
            if land.get("landIndex") == land_index:
                lands[i] = updated
                return
        lands.append(updated)

    async def complete_charge_task(self, active, remaining):
        while remaining > 0:
            if self.no_energy:
                return
            immature = sorted(
                (
                    land
                    for land in active
                    if land.get("status") == 3
                    and (land.get("plant") or {}).get("plantId")
                ),
                key=self._land_maturity_key,
                reverse=True,
            )
            if not immature:
                self.tlog("未成熟作物不足，提前结束10次充能补足")
                return
            progressed = False
            for land in immature:
                if remaining <= 0 or self.no_energy:
                    return
                result = await self.charge_land(land)
                if result:
                    self.replace_land(active, result)
                    remaining -= 1
                    progressed = True
                await asyncio.sleep(1)
            if not progressed:
                self.tlog("充能未成功，提前结束10次充能补足")
                return

    async def _harvest_mature_lands(self, active):
        updated = []
        for land in active:
            if land.get("status") == 2:
                replanted = await self.harvest_and_replant(land)
                if replanted:
                    updated.append(replanted)
            else:
                updated.append(land)
        return updated

    async def farm_tasks(self, tasks):
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
        lands = await self.get_lands()
        active = await self.ensure_planted_lands(lands)
        active = await self._harvest_mature_lands(active)
        if not charge_pending and not land_pending and not harvest_pending:
            return
        done_land = max(safe_int((land_pending or {}).get("doneValue")), 0)
        need_land = max(
            safe_int((land_pending or {}).get("finishValue")) - done_land,
            0,
        )
        if not active:
            self.tlog("没有可充能作物")
            return
        charged = 0
        immature_idx = [i for i, land in enumerate(active) if land.get("status") == 3]
        if need_land > 0 and immature_idx:
            offset = done_land % len(immature_idx)
            rotated = immature_idx[offset:] + immature_idx[:offset]
            for idx in rotated[:need_land]:
                if self.no_energy:
                    break
                result = await self.charge_land(active[idx])
                if result:
                    active[idx] = result
                    charged += 1
                    await asyncio.sleep(1)
        need_charge = max(
            safe_int((charge_pending or {}).get("finishValue"))
            - safe_int((charge_pending or {}).get("doneValue"))
            - charged,
            0,
        )
        await self.complete_charge_task(active, need_charge)
        for land in sorted(active, key=self._land_maturity_key, reverse=True):
            await self.grow_land_to_harvest(land)

    async def run(self):
        if not await self.login():
            return
        claimed = set()
        if not self.newbie_done():
            await self.prepare_newbie_energy(claimed)
        if not self.newbie_done() and not await self.newbie_task():
            return
        await self.sign()
        tasks = await self.get_tasks()
        await self.claim_ready_tasks(tasks, claimed)
        jumped = await self.do_jump_tasks(tasks)
        garbaged = await self.do_garbage_task(tasks)
        if jumped or garbaged:
            if not garbaged:
                await asyncio.sleep(1)
            tasks = await self.get_tasks()
            await self.claim_ready_tasks(tasks, claimed)
        self.no_energy = False
        self.no_buy_energy = False
        for _ in range(3):
            await self.farm_tasks(tasks)
            tasks = await self.get_tasks()
            newly_claimed = await self.claim_ready_tasks(tasks, claimed)
            if newly_claimed <= 0:
                break
            self.no_energy = False
            self.no_buy_energy = False
            has_undo_farm = any(
                t.get("taskStatus") == "UNDO"
                and any(
                    k in t.get("taskTitle", "")
                    for k in ("10次作物充能", "三块不同", "收获一次作物")
                )
                for t in tasks
            )
            if not has_undo_farm:
                lands = await self.get_lands()
                active = await self.ensure_planted_lands(lands)
                await self._harvest_mature_lands(active)
                break
