"""权益超市、安全管家、签到区、通通乡村、云手机、沃阅读与新疆联通任务回归测试。"""

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from cu_tasks.config import Settings
from cu_tasks.session import Account
from cu_tasks.tasks.farm import NEWBIE_STEPS, FarmTask
from cu_tasks.tasks.market import MarketTask
from cu_tasks.tasks.security import SecurityTask
from cu_tasks.tasks.sign import TASK_BROWSE_TIMEOUT, SignTask
from cu_tasks.tasks.uphone import UphoneTask
from cu_tasks.tasks.woread import JF_SIGN_TASK, WoreadTask
from cu_tasks.tasks.xinjiang import XinjiangTask


def make_account(province="新疆"):
    account = Account(
        "test-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    account.mobile = "13100000309"
    account.ecs_token = "ecs-test-token"
    account.province = province
    return account


class MarketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.account = make_account()
        self.addAsyncCleanup(self.account.close)
        self.task = MarketTask(self.account, Settings(market_wait_max=300))
        self.addAsyncCleanup(self.task.close)
        self.task.token = "jwt-token"
        self.messages = []
        self.task.tlog = self.messages.append

    async def test_early_morning_skips_before_any_prefetch_requests(self):
        self.task.fetch_select_info = AsyncMock()
        self.task.fetch_youchoice_activity = AsyncMock()
        with (
            patch.object(MarketTask, "is_open", return_value=False),
            patch.object(MarketTask, "seconds_until_open", return_value=10800.0),
        ):
            await self.task.privilege()
        self.task.fetch_select_info.assert_not_awaited()
        self.task.fetch_youchoice_activity.assert_not_awaited()
        self.assertTrue(any("跳过优享权益" in m for m in self.messages))

    async def test_prewarm_sleeps_until_near_open_then_prefetches_and_grabs(self):
        events = []

        async def fake_sleep(seconds):
            events.append(("sleep", round(seconds, 1)))

        async def fake_info():
            events.append(("select_info",))
            return {"state": 1}

        async def fake_act():
            events.append(("activity",))
            return {
                "activityId": 99,
                "isUnlock": 0,
                "userAvailableTimes": 1,
                "detailList": [
                    {
                        "id": 1,
                        "productName": "哔哩哔哩大会员周卡",
                        "isSurprise": 1,
                        "status": 1,
                        "leftQuantity": 50,
                    },
                    {
                        "id": 2,
                        "productName": "酷狗音乐豪华VIP-1天卡",
                        "isSurprise": 0,
                        "status": 1,
                        "leftQuantity": 100,
                    },
                ],
            }

        async def fake_receive(act, item):
            events.append(("receive", item["productName"]))
            return {"code": 200, "msg": "成功"}

        self.task.fetch_select_info = fake_info
        self.task.fetch_youchoice_activity = fake_act
        self.task.receive_rights = fake_receive
        open_states = iter([False, True])
        remain_times = iter([118.0, 7.5])
        with (
            patch("cu_tasks.tasks.market.asyncio.sleep", side_effect=fake_sleep),
            patch.object(MarketTask, "is_open", side_effect=lambda: next(open_states)),
            patch.object(
                MarketTask, "seconds_until_open", side_effect=lambda: next(remain_times)
            ),
        ):
            await self.task.privilege()

        self.assertEqual(
            events,
            [
                ("sleep", 106.0),
                ("select_info",),
                ("activity",),
                ("sleep", 7.5),
                ("receive", "哔哩哔哩大会员周卡"),
            ],
        )
        self.assertTrue(any("成功(10点直发)" in m for m in self.messages))

        # 慢响应跨过 10:00:00 时（seconds_until_open 返回 0.0）不额外休眠，直接抢购
        events.clear()
        self.messages.clear()
        open_states = iter([False, True])
        remain_times = iter([118.0, 0.0])
        with (
            patch("cu_tasks.tasks.market.asyncio.sleep", side_effect=fake_sleep),
            patch.object(MarketTask, "is_open", side_effect=lambda: next(open_states)),
            patch.object(
                MarketTask, "seconds_until_open", side_effect=lambda: next(remain_times)
            ),
        ):
            await self.task.privilege()
        self.assertEqual(
            events,
            [
                ("sleep", 106.0),
                ("select_info",),
                ("activity",),
                ("receive", "哔哩哔哩大会员周卡"),
            ],
        )

    async def test_fast_grab_handles_man_machine_and_stop_keywords(self):
        act = {
            "activityId": 88,
            "isUnlock": 1,
            "userAvailableTimes": 1,
            "detailList": [
                {
                    "id": 10,
                    "productName": "商品A",
                    "isSurprise": 0,
                    "status": 1,
                    "leftQuantity": 10,
                },
                {
                    "id": 11,
                    "productName": "商品B",
                    "isSurprise": 0,
                    "status": 1,
                    "leftQuantity": 10,
                },
            ],
        }
        self.task.fetch_select_info = AsyncMock(return_value={"state": 1})
        self.task.fetch_youchoice_activity = AsyncMock(return_value=act)
        self.task.man_machine = AsyncMock(return_value=True)
        self.task.receive_rights = AsyncMock(
            side_effect=[{"code": -2, "msg": "需验证"}, {"code": 200, "msg": "ok"}]
        )
        open_states = iter([False, True])
        with (
            patch("cu_tasks.tasks.market.asyncio.sleep", new=AsyncMock()),
            patch.object(MarketTask, "is_open", side_effect=lambda: next(open_states)),
            patch.object(MarketTask, "seconds_until_open", return_value=1.0),
        ):
            await self.task.privilege()
        self.task.man_machine.assert_awaited_once_with(88)
        self.assertEqual(self.task.fetch_youchoice_activity.await_count, 1)
        self.assertTrue(any("成功(10点直发)" in m for m in self.messages))

        # manMachine 重试后返回 STOP_KEYWORDS（如“今日已领”）应立即停止，不继续扫后续商品
        self.messages.clear()
        self.task.fetch_youchoice_activity.reset_mock()
        self.task.receive_rights = AsyncMock(
            side_effect=[
                {"code": -2, "msg": "需验证"},
                {"code": 500, "msg": "今日已领"},
            ]
        )
        open_states = iter([False, True])
        with (
            patch("cu_tasks.tasks.market.asyncio.sleep", new=AsyncMock()),
            patch.object(MarketTask, "is_open", side_effect=lambda: next(open_states)),
            patch.object(MarketTask, "seconds_until_open", return_value=1.0),
        ):
            await self.task.privilege()
        self.assertEqual(self.task.receive_rights.await_count, 2)
        self.assertEqual(self.task.fetch_youchoice_activity.await_count, 1)
        self.assertFalse(any("降级走常规流程" in m for m in self.messages))


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.account = make_account()
        self.addAsyncCleanup(self.account.close)
        self.task = SecurityTask(self.account)
        self.addAsyncCleanup(self.task.close)
        self.messages = []
        self.task.tlog = self.messages.append
        self.task.edop_ticket = AsyncMock(return_value="edop-tk")
        self.task.uca_auth = AsyncMock(return_value="sa-tk")
        self.task.points_ticket = AsyncMock(return_value="pt-tk")

    async def test_auto_issued_reward_4012_is_not_logged_as_failure(self):
        post_mock = AsyncMock(
            side_effect=[
                {"code": 200, "data": {"code": "0000", "msg": "ok"}},
                {
                    "code": 200,
                    "data": {"code": "4012", "msg": "奖励类型为自动发放"},
                },
            ]
        )
        with (
            patch(
                "cu_tasks.tasks.security.JfTaskClient.init",
                AsyncMock(return_value=True),
            ),
            patch(
                "cu_tasks.tasks.security.JfTaskClient.task_list",
                AsyncMock(
                    return_value=[
                        {
                            "taskName": "联通助理-签到1",
                            "taskCode": "s1",
                            "finishCount": 0,
                            "needCount": 1,
                        }
                    ]
                ),
            ),
            patch("cu_tasks.tasks.security.JfTaskClient.post", post_mock),
        ):
            await self.task.run()
        self.assertEqual(post_mock.await_count, 2)
        self.assertIn("签到成功", self.messages)
        self.assertIn("奖励已自动发放", self.messages)
        self.assertFalse(any("失败" in m for m in self.messages))

    async def test_non_paid_user_4016_skips_without_calling_receive_or_logging_failure(
        self,
    ):
        post_mock = AsyncMock(
            return_value={
                "code": 200,
                "data": {
                    "code": "4016",
                    "msg": "签到任务为联通助理/联通安全管家付费用户成长体系",
                },
            }
        )
        with (
            patch(
                "cu_tasks.tasks.security.JfTaskClient.init",
                AsyncMock(return_value=True),
            ),
            patch(
                "cu_tasks.tasks.security.JfTaskClient.task_list",
                AsyncMock(
                    return_value=[
                        {
                            "taskName": "联通助理-签到1",
                            "taskCode": "s1",
                            "finishCount": 0,
                            "needCount": 1,
                        }
                    ]
                ),
            ),
            patch("cu_tasks.tasks.security.JfTaskClient.post", post_mock),
        ):
            await self.task.run()
        post_mock.assert_awaited_once()
        self.assertTrue(any("非付费会员跳过签到" in m for m in self.messages))
        self.assertFalse(any("失败" in m for m in self.messages))


class SignTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.account = make_account()
        self.addAsyncCleanup(self.account.close)
        self.task = SignTask(self.account)
        self.addAsyncCleanup(self.task.close)
        self.messages = []
        self.task.tlog = self.messages.append

    async def test_batches_do_and_claim_tasks_without_infinite_retry_on_failure(self):
        list_calls = 0

        async def fake_req(method, url, **kw):
            nonlocal list_calls
            if url.endswith("/taskList"):
                list_calls += 1
                if list_calls == 1:
                    return {
                        "code": 200,
                        "data": {
                            "code": "0000",
                            "data": {
                                "taskList": [
                                    {
                                        "id": "t1",
                                        "taskName": "任务1",
                                        "taskState": "1",
                                        "taskType": "5",
                                        "url": "1",
                                    },
                                    {
                                        "id": "t2",
                                        "taskName": "失败任务",
                                        "taskState": "1",
                                        "taskType": "5",
                                        "url": "1",
                                    },
                                ]
                            },
                        },
                    }
                if list_calls == 2:
                    # t1 转可领，t2 仍保持未完成("1")，不应死循环重做 t2
                    return {
                        "code": 200,
                        "data": {
                            "code": "0000",
                            "data": {
                                "taskList": [
                                    {
                                        "id": "t1",
                                        "taskName": "任务1",
                                        "taskState": "0",
                                        "taskType": "5",
                                    },
                                    {
                                        "id": "t2",
                                        "taskName": "失败任务",
                                        "taskState": "1",
                                        "taskType": "5",
                                    },
                                ]
                            },
                        },
                    }
                return {
                    "code": 200,
                    "data": {
                        "code": "0000",
                        "data": {
                            "taskList": [
                                {
                                    "id": "t2",
                                    "taskName": "失败任务",
                                    "taskState": "1",
                                    "taskType": "5",
                                }
                            ]
                        },
                    },
                }
            if url.endswith("/gettaskip"):
                return {"code": 200, "data": {}}
            if url.endswith("/completeTask"):
                tid = kw["params"]["taskId"]
                return {
                    "code": 200,
                    "data": {"code": "0000" if tid == "t1" else "9999", "desc": "err"},
                }
            if url.endswith("/getTaskReward"):
                return {
                    "code": 200,
                    "data": {
                        "code": "0000",
                        "data": {
                            "code": "0000",
                            "prizeName": "话费红包",
                            "prizeNameRed": "+0.01元",
                        },
                    },
                }
            raise AssertionError(f"unexpected url: {url}")

        self.task.req = fake_req
        with patch("cu_tasks.tasks.sign.asyncio.sleep", new=AsyncMock()):
            acted = await self.task.get_task_list()
        self.assertTrue(acted)
        self.assertEqual(list_calls, 2)
        self.assertTrue(any("+0.01元" in m for m in self.messages))
        self.assertTrue(any("所有任务处理完毕" in m for m in self.messages))

    async def test_do_task_external_url_uses_short_browse_timeout(self):
        seen_timeouts = []

        async def fake_req(method, url, **kw):
            if url.startswith("https://third.example.com"):
                seen_timeouts.append(kw.get("timeout"))
                return {"code": -1, "headers": {}, "data": None, "error": "timeout"}
            if url.endswith("/gettaskip"):
                return {"code": 200, "data": {}}
            if url.endswith("/completeTask"):
                return {"code": 200, "data": {"code": "0000", "desc": "ok"}}
            raise AssertionError(f"unexpected url: {url}")

        self.task.req = fake_req
        with patch("cu_tasks.tasks.sign.asyncio.sleep", new=AsyncMock()):
            ok, busy = await self.task.do_task_from_list(
                {
                    "id": "t1",
                    "taskName": "外部浏览",
                    "url": "https://third.example.com/p",
                }
            )
        self.assertEqual((ok, busy), (True, False))
        self.assertEqual(seen_timeouts, [TASK_BROWSE_TIMEOUT])

    async def test_no_change_run_reuses_initial_telephone_snapshot(self):
        tel_calls = 0

        async def fake_req(method, url, **kw):
            nonlocal tel_calls
            if url.endswith("/getTelephone"):
                tel_calls += 1
                return {
                    "code": 200,
                    "data": {
                        "status": "0000",
                        "data": {
                            "telephone": "15.41",
                            "needexpNumber": "10.71",
                            "month": "11",
                        },
                    },
                }
            if url.endswith("/getContinuous"):
                return {
                    "code": 200,
                    "data": {"code": "0000", "data": {"todayIsSignIn": "y"}},
                }
            if url.endswith("/getMonthSign"):
                return {"code": 200, "data": {"code": "0000", "data": {"taskList": []}}}
            if url.endswith("/taskList"):
                return {"code": 200, "data": {"code": "0000", "data": {"taskList": []}}}
            if url.endswith("/phoneDetails"):
                return {
                    "code": 200,
                    "data": {"status": "0000", "data": {"detailedBO": []}},
                }
            raise AssertionError(f"unexpected url: {url}")

        self.task.req = fake_req
        await self.task.run()
        self.assertEqual(tel_calls, 1)
        self.assertTrue(any("10.71元 将于 11月底到期" in m for m in self.messages))


class FarmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.account = make_account()
        self.addAsyncCleanup(self.account.close)
        self.task = FarmTask(
            self.account, Settings(farm_garbage_wait=0, farm_harvest_wait=0)
        )
        self.addAsyncCleanup(self.task.close)
        self.messages = []
        self.task.tlog = self.messages.append

    async def test_claims_jump_and_garbage_energy_before_charging_and_replants_after_harvest_reward(
        self,
    ):
        order = []
        self.task.newbie_list = list(NEWBIE_STEPS)
        self.task.login = AsyncMock(return_value=True)
        self.task.sign = AsyncMock()
        t_jump = {
            "taskCode": "J1",
            "taskTitle": "浏览",
            "taskType": "GAME",
            "taskStatus": "UNDO",
            "jumpUrl": "https://x",
        }
        t_jump_ready = {**t_jump, "taskStatus": "UNCLA", "carbonEnergyAmount": 50}
        t_harvest_ready = {
            "taskCode": "H1",
            "taskTitle": "本周成功收获一次作物",
            "taskStatus": "UNCLA",
            "carbonEnergyAmount": 200,
        }

        self.task.get_tasks = AsyncMock(
            side_effect=[
                [t_jump],
                [t_jump_ready],
                [t_harvest_ready],
            ]
        )

        async def fake_claim(tasks, claimed=None):
            count = 0
            for t in tasks:
                if t.get("taskStatus") == "UNCLA" and t["taskCode"] not in claimed:
                    claimed.add(t["taskCode"])
                    order.append(f"claim:{t['taskCode']}")
                    count += 1
            return count

        async def fake_farm_tasks(tasks):
            order.append("farm_tasks")
            self.task.no_energy = True

        async def fake_replant(lands):
            order.append(f"replant:no_energy={self.task.no_energy}")
            return lands

        self.task.claim_ready_tasks = fake_claim
        self.task.do_jump_tasks = AsyncMock(return_value=1)
        self.task.do_garbage_task = AsyncMock(return_value=True)
        self.task.farm_tasks = fake_farm_tasks
        self.task.get_lands = AsyncMock(return_value=[{"landIndex": 1, "status": 1}])
        self.task.ensure_planted_lands = fake_replant

        with patch("cu_tasks.tasks.farm.asyncio.sleep", new=AsyncMock()):
            await self.task.run()

        self.assertEqual(
            order,
            ["claim:J1", "farm_tasks", "claim:H1", "replant:no_energy=False"],
        )

    async def test_do_task_retries_on_consecutive_transient_upstream_resets(self):
        self.task.post = AsyncMock(
            side_effect=[
                {"code": 1, "msg": "I/O error: Connection reset"},
                {"code": 1, "msg": "I/O error: Connection reset"},
                {"code": 0, "msg": "ok"},
            ]
        )
        with patch("cu_tasks.tasks.farm.asyncio.sleep", new=AsyncMock()):
            ok = await self.task.do_task({"taskCode": "J1", "taskTitle": "浏览钱包"})
        self.assertTrue(ok)
        self.assertEqual(self.task.post.await_count, 3)
        self.assertIn("已执行[浏览钱包]", self.messages)

    async def test_low_carbon_skips_guaranteed_failures_and_tracks_deductions(self):
        # 25g (<50g 单次充能、<100g 购种)：不发必败请求，且不把 25g 清零破坏后续累加
        self.task.carbon = 25
        self.task.post = AsyncMock()
        land = {"landIndex": 1, "status": 3, "plant": {"plantId": "P1"}}
        self.assertIsNone(await self.task.charge_land(land))
        await self.task.ensure_planted_lands(
            [{"landIndex": 1, "status": 1}, {"landIndex": 2, "status": 1}]
        )
        self.task.post.assert_not_awaited()
        self.assertEqual(self.task.carbon, 25)

        # 领取 +50g（且练习 finish_task 瞬断重试）后变为 75g：可充能 1 次扣减至 25g，第 2 次自动跳过
        self.task.no_energy = False
        self.task.no_buy_energy = False
        self.task.post = AsyncMock(
            side_effect=[
                {"code": 1, "msg": "I/O error: Connection reset"},
                {"code": 0, "msg": "ok"},
                {"code": 0, "data": {"landIndex": 1, "status": 3}},
            ]
        )
        with patch("cu_tasks.tasks.farm.asyncio.sleep", new=AsyncMock()):
            self.assertTrue(
                await self.task.finish_task(
                    {
                        "taskCode": "T1",
                        "taskTitle": "浏览任务",
                        "carbonEnergyAmount": 50,
                    }
                )
            )
        self.assertEqual(self.task.carbon, 75)
        self.assertIsNotNone(await self.task.charge_land(land))
        self.assertEqual(self.task.carbon, 25)
        self.assertIsNone(await self.task.charge_land(land))
        self.assertEqual(self.task.post.await_count, 3)

        # 50g 虽不足购种(100g)，但不置位 no_energy，仍允许已有作物充能
        self.task.carbon = 50
        self.task.no_energy = False
        self.task.no_buy_energy = False
        self.task.post = AsyncMock(
            return_value={"code": 0, "data": {"landIndex": 2, "status": 3}}
        )
        active = await self.task.ensure_planted_lands(
            [
                {"landIndex": 1, "status": 1},
                {"landIndex": 2, "status": 3, "plant": {"plantId": "P1"}},
            ]
        )
        self.task.post.assert_not_awaited()
        self.assertFalse(self.task.no_energy)
        self.assertIsNotNone(await self.task.charge_land(active[0]))
        self.assertEqual(self.task.carbon, 0)

        # 余额未知时服务端返回能量不足：仅尝试第 1 块空地即停止，且不记为“失败”
        self.task.carbon = None
        self.task.no_energy = False
        self.task.no_buy_energy = False
        self.task.post = AsyncMock(
            side_effect=[
                {"code": 0, "data": {"list": [{"itemNo": "P1"}]}},
                {"code": -1, "msg": "用户碳能量不足"},
            ]
        )
        await self.task.ensure_planted_lands(
            [{"landIndex": 1, "status": 1}, {"landIndex": 2, "status": 1}]
        )
        self.assertEqual(self.task.post.await_count, 2)
        self.assertFalse(any("失败" in m for m in self.messages))

    async def test_newly_claimed_task_energy_continues_unfinished_charge_task(self):
        self.task.newbie_list = list(NEWBIE_STEPS)
        self.task.login = AsyncMock(return_value=True)
        self.task.sign = AsyncMock()
        t_charge_undo = {
            "taskCode": "C10",
            "taskTitle": "完成10次作物充能",
            "taskStatus": "UNDO",
        }
        t_land_ready = {
            "taskCode": "L3",
            "taskTitle": "给三块不同的地块充能",
            "taskStatus": "UNCLA",
            "carbonEnergyAmount": 50,
        }
        t_charge_ready = {
            "taskCode": "C10",
            "taskTitle": "完成10次作物充能",
            "taskStatus": "UNCLA",
            "carbonEnergyAmount": 100,
        }
        self.task.get_tasks = AsyncMock(
            side_effect=[
                [t_charge_undo],
                [t_charge_undo, t_land_ready],
                [t_charge_ready],
            ]
        )
        self.task.do_jump_tasks = AsyncMock(return_value=0)
        self.task.do_garbage_task = AsyncMock(return_value=False)
        self.task.finish_task = AsyncMock(return_value=True)
        self.task.farm_tasks = AsyncMock()
        self.task.get_lands = AsyncMock(return_value=[])
        self.task.ensure_planted_lands = AsyncMock(return_value=[])

        await self.task.run()
        self.assertEqual(self.task.farm_tasks.await_count, 2)
        self.assertEqual(self.task.finish_task.await_count, 2)

    async def test_sign_retries_transient_user_error_and_never_false_reports_sign_success(
        self,
    ):
        today = "2026-11-18 00:47:15"
        self.task.post = AsyncMock(
            side_effect=[
                {"code": 0, "data": {"signinCode": "S1"}},
                ValueError("POST /client/v1/sign/user | 网络/请求异常"),
                {"code": 1, "msg": "Read timed out"},
                {"code": 0, "data": {"lastSigninTime": today}},
            ]
        )
        with (
            patch("cu_tasks.tasks.farm.asyncio.sleep", new=AsyncMock()),
            patch("cu_tasks.tasks.farm.datetime") as dt,
        ):
            dt.now.return_value.strftime.return_value = "2026-11-18"
            await self.task.sign()
        self.assertIn("今日已签到", self.messages)
        self.assertFalse(any("签到成功" in m for m in self.messages))

        # 连续 3 次失败返回 code!=0 时，不应把空 lastSigninTime 误判为未签到并发起假 signIn
        self.messages.clear()
        self.task.post = AsyncMock(
            side_effect=[
                {"code": 0, "data": {"signinCode": "S1"}},
                {"code": 1, "msg": "I/O error: Connection reset", "data": None},
                {"code": 1, "msg": "I/O error: Connection reset", "data": None},
                {"code": 1, "msg": "I/O error: Connection reset", "data": None},
            ]
        )
        with patch("cu_tasks.tasks.farm.asyncio.sleep", new=AsyncMock()):
            await self.task.sign()
        self.assertTrue(any("查询签到状态失败" in m for m in self.messages))
        self.assertFalse(any("签到成功" in m for m in self.messages))

    async def test_farm_tasks_harvests_mature_rotates_distinct_lands_and_prioritizes_near_mature(
        self,
    ):
        self.task.carbon = 200
        lands = [
            {
                "landIndex": 1,
                "status": 3,
                "plant": {"plantId": "P1", "curLevel": "3", "curCarbonNum": 100},
            },
            {
                "landIndex": 2,
                "status": 3,
                "plant": {"plantId": "P1", "curLevel": "3", "curCarbonNum": 450},
            },
            {
                "landIndex": 3,
                "status": 3,
                "plant": {"plantId": "P1", "curLevel": "1", "curCarbonNum": 0},
            },
            {
                "landIndex": 4,
                "status": 2,
                "plant": {"plantId": "P1", "curLevel": "4", "curCarbonNum": 600},
            },
        ]
        charged_order = []
        harvested = []

        async def fake_charge(land, mock=None):
            if self.task.carbon < 50 or land.get("status") == 2:
                self.task.no_energy = True
                return None
            self.task.carbon -= 50
            if self.task.carbon < 50:
                self.task.no_energy = True
            charged_order.append(land["landIndex"])
            return land

        async def fake_harvest_replant(land):
            harvested.append(land["landIndex"])
            return None

        self.task.get_lands = AsyncMock(return_value=lands)
        self.task.ensure_planted_lands = AsyncMock(return_value=list(lands))
        self.task.charge_land = fake_charge
        self.task.harvest_and_replant = fake_harvest_replant
        with patch("cu_tasks.tasks.farm.asyncio.sleep", new=AsyncMock()):
            await self.task.farm_tasks(
                [
                    {
                        "taskTitle": "给三块不同的地块充能",
                        "taskStatus": "UNDO",
                        "doneValue": 1,
                        "finishValue": 3,
                    },
                    {
                        "taskTitle": "本周成功收获一次作物",
                        "taskStatus": "UNDO",
                    },
                ]
            )
        # 先收成熟地块4；“三块不同”跳过已充过的第1块去充地块2、3；催熟优先充进度最高的地块2
        self.assertEqual(harvested, [4])
        self.assertEqual(charged_order, [2, 3, 2, 2])


class UphoneAndWoreadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.account = make_account()
        self.addAsyncCleanup(self.account.close)

    async def test_uphone_batches_obtain_tasks_and_completes_repeatable_tasks(self):
        task = UphoneTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.cp_token = "eyJ.cp"
        task.usr_token = "usr"
        task.activity_login = AsyncMock(return_value=True)
        task.task_logs = AsyncMock(return_value=(True, {"code": 0}))
        task.raffle_get = AsyncMock(return_value=(True, {"code": 0}))
        task.points_summary = AsyncMock(return_value={"balanceScoreNum": 500})
        task.task_list = AsyncMock(
            side_effect=[
                # 初始：任务A + 日内可重复任务“观看广告”(第1次)
                (
                    [
                        {
                            "taskCode": "A1",
                            "taskName": "任务A",
                            "status": "INCOMPLETE",
                            "pointsCount": 5,
                        },
                        {
                            "taskCode": "20260730",
                            "taskName": "观看广告",
                            "status": "INCOMPLETE",
                            "pointsCount": 2,
                        },
                    ],
                    0,
                ),
                # 第1轮上报后均变为可领
                (
                    [
                        {
                            "taskCode": "A1",
                            "taskName": "任务A",
                            "status": "UNCLAIMED",
                            "pointsCount": 5,
                        },
                        {
                            "taskCode": "20260730",
                            "taskName": "观看广告",
                            "status": "UNCLAIMED",
                            "pointsCount": 2,
                        },
                    ],
                    0,
                ),
                # 领完第1轮后，“观看广告”回到 INCOMPLETE（第2次）
                (
                    [
                        {
                            "taskCode": "A1",
                            "taskName": "任务A",
                            "status": "COMPLETED",
                            "pointsCount": 5,
                        },
                        {
                            "taskCode": "20260730",
                            "taskName": "观看广告",
                            "status": "INCOMPLETE",
                            "pointsCount": 2,
                        },
                    ],
                    0,
                ),
                # 第2轮上报后可领
                (
                    [
                        {
                            "taskCode": "20260730",
                            "taskName": "观看广告",
                            "status": "UNCLAIMED",
                            "pointsCount": 2,
                        }
                    ],
                    0,
                ),
                # 全部完成
                (
                    [
                        {
                            "taskCode": "20260730",
                            "taskName": "观看广告",
                            "status": "COMPLETED",
                            "pointsCount": 2,
                        }
                    ],
                    0,
                ),
            ]
        )
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            bal = await task.obtain_tasks()
            draws = await task.winter_lottery(initial_left=0)

        self.assertEqual(bal, 500)
        self.assertEqual(draws, 0)
        self.assertEqual(task.raffle_get.await_count, 3)
        self.assertEqual(task.task_list.await_count, 5)
        self.assertFalse(any("抽奖" in m for m in messages))

    async def test_uphone_repolls_stale_balance_crossing_lottery_threshold_and_skips_empty_winter(
        self,
    ):
        task = UphoneTask(self.account, Settings(uphone_lottery_cost=100))
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.cp_token = "eyJ.cp"
        task.usr_token = "usr"
        task.activity_login = AsyncMock(return_value=True)
        task.raffle_get = AsyncMock(return_value=(True, {"code": 0}))
        task.task_list = AsyncMock(
            side_effect=[
                (
                    [
                        {
                            "taskCode": "A1",
                            "taskName": "任务A",
                            "status": "UNCLAIMED",
                            "pointsCount": 10,
                        }
                    ],
                    0,
                ),
                ([], 0),
                ([], 0),
            ]
        )
        # 首次 summary 仍返回入账前旧余额 95（95 < 100 <= 95+10），补查后拿到落库余额 105
        task.points_summary = AsyncMock(
            side_effect=[{"balanceScoreNum": 95}, {"balanceScoreNum": 105}]
        )
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            bal = await task.obtain_tasks()
            left = await task.winter_claim()
            draws = await task.winter_lottery(initial_left=left)
        self.assertEqual(bal, 105)
        self.assertEqual((left, draws), (0, 0))
        self.assertEqual(task.points_summary.await_count, 2)
        self.assertFalse(any("抽奖" in m for m in messages))

    async def test_woread_tofinish_requires_finish_count_increase_before_reporting_success(
        self,
    ):
        task = WoreadTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        sign_done = {
            "taskCode": JF_SIGN_TASK,
            "taskName": "每日签到",
            "finishCount": 1,
            "needCount": 1,
        }
        read_pending = {
            "taskCode": "read4m",
            "taskName": "登录并读听看4分钟",
            "finishCount": 0,
            "needCount": 1,
        }
        read_done = {**read_pending, "finishCount": 1}
        jf = AsyncMock()
        jf.post = AsyncMock(return_value={"code": 200, "data": {"data": True}})
        jf.user_info = AsyncMock(
            return_value={
                "todayEarnScore": 3,
                "availableScore": 100,
                "allEarnScore": 50,
            }
        )
        # 第 1 次：toFinish 返回 data=True 但读回 finishCount 仍为 0（仅读 2 分钟）→ 不虚报成功
        jf.task_list = AsyncMock(return_value=[sign_done, read_pending])
        with patch("cu_tasks.tasks.woread.asyncio.sleep", new=AsyncMock()):
            await task.claim_points(
                jf, tasks=[sign_done, read_pending], sign_task=sign_done
            )
        self.assertIn("领积分 [登录并读听看4分钟]: 未达条件", messages)
        self.assertIn("结束: 共领 0 项积分", messages)

        # 第 2 次：读回 finishCount 增至 1 → 真到账成功
        messages.clear()
        jf.task_list = AsyncMock(return_value=[sign_done, read_done])
        with patch("cu_tasks.tasks.woread.asyncio.sleep", new=AsyncMock()):
            await task.claim_points(
                jf, tasks=[sign_done, read_pending], sign_task=sign_done
            )
        self.assertIn("领积分 [登录并读听看4分钟]: 成功", messages)
        self.assertIn("结束: 共领 1 项积分", messages)

    async def test_woread_skips_reading_and_sign_post_when_all_daily_tasks_completed(
        self,
    ):
        task = WoreadTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.auth = AsyncMock(return_value=True)
        task.well_ticket = AsyncMock(
            return_value=("https://m.jf.10010.com/?ticket=t", "t")
        )
        task.report_reading = AsyncMock()
        post_mock = AsyncMock()

        completed_tasks = [
            {
                "taskCode": JF_SIGN_TASK,
                "taskName": "每日签到",
                "finishCount": 1,
                "needCount": 1,
            },
            {
                "taskCode": "read4m",
                "taskName": "登录并读听看4分钟",
                "finishCount": 2,
                "needCount": 2,
            },
        ]
        with (
            patch(
                "cu_tasks.tasks.woread.JfTaskClient.init", AsyncMock(return_value=True)
            ),
            patch(
                "cu_tasks.tasks.woread.JfTaskClient.task_list",
                AsyncMock(return_value=completed_tasks),
            ),
            patch("cu_tasks.tasks.woread.JfTaskClient.post", post_mock),
            patch(
                "cu_tasks.tasks.woread.JfTaskClient.user_info",
                AsyncMock(
                    return_value={
                        "todayEarnScore": 12,
                        "availableScore": 2000,
                        "allEarnScore": 1600,
                    }
                ),
            ),
        ):
            await task.run()

        task.report_reading.assert_not_awaited()
        post_mock.assert_not_awaited()
        self.assertIn("今日已签到", messages)
        self.assertTrue(any("跳过阅读上报" in m for m in messages))

    async def test_xinjiang_logs_error_when_login_returns_invalid_body(self):
        task = XinjiangTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.get_ticket = AsyncMock(return_value="tk")
        task.req = AsyncMock(
            return_value={"code": 502, "headers": {}, "data": "Bad Gateway"}
        )
        self.assertFalse(await task.login())
        self.assertTrue(any("请求失败" in m for m in messages))

    async def test_xinjiang_monthly_draw_relogs_session_on_refresh_hint(self):
        task = XinjiangTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.login = AsyncMock(return_value=True)
        task.comm_high_flag = AsyncMock(return_value="true")
        task.post = AsyncMock(
            side_effect=[
                {"code": -1, "data": "操作异常，请刷新页面重试！"},
                {"code": 0, "msg": "thanks1", "data": "很遗憾，未中奖！"},
            ]
        )
        with patch("cu_tasks.tasks.xinjiang.asyncio.sleep", new=AsyncMock()):
            new_flag = await task.monthly_draw("Nov2026Act", "ThemeNov2026Act", "false")
        task.login.assert_awaited_once()
        task.comm_high_flag.assert_awaited_once_with("ThemeNov2026Act")
        self.assertEqual(new_flag, "true")
        self.assertEqual(task.post.await_args_list[1].args[1]["commHighFlag"], "true")
        self.assertIn("每月抽奖: 很遗憾，未中奖！", messages)

    async def test_xinjiang_maps_prize_name_and_stops_second_daka_when_already_checked_in(
        self,
    ):
        task = XinjiangTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.login = AsyncMock(return_value=True)
        task.comm_high_flag = AsyncMock(return_value="false")
        task.post = AsyncMock(
            side_effect=[
                {"code": "SUCCESS", "msg": "ltypzxhynk", "data": ""},
                {"code": "SUCCESS", "msg": "ok"},
                {
                    "code": -1,
                    "msg": "fail",
                    "data": "您已打卡成功，每个用户只能打卡一次哦~",
                },
            ]
        )
        sleep_mock = AsyncMock()
        with patch("cu_tasks.tasks.xinjiang.asyncio.sleep", new=sleep_mock):
            await task.run()
        self.assertIn("每月抽奖中奖: 联通云盘尊享会员-年卡", messages)
        # draw_SMS 仍传原始 prizeId
        self.assertEqual(
            task.post.await_args_list[1].args,
            (
                "marchAct/draw_SMS",
                {
                    "activityId": task.post.await_args_list[0].args[1]["activityId"],
                    "prizeId": "ltypzxhynk",
                },
            ),
        )
        # 打卡1返回“每个用户只能打卡一次”后立即结束，不再调打卡2
        self.assertEqual(task.post.await_count, 3)
        self.assertEqual(sleep_mock.await_count, 1)
        self.assertFalse(any("每日打卡2" in m for m in messages))

    async def test_uphone_waits_for_sign_points_settlement_when_no_obtain_task_claimed(
        self,
    ):
        task = UphoneTask(self.account, Settings(uphone_lottery_cost=100))
        self.addAsyncCleanup(task.close)
        task.cp_token = "eyJ.cp"
        task.usr_token = "usr"
        task.req = AsyncMock(
            return_value={"code": 200, "data": {"code": 0, "msg": "签到成功"}}
        )
        task.activity_login = AsyncMock(return_value=True)
        task.task_list = AsyncMock(return_value=([], 0))
        task.points_summary = AsyncMock(
            side_effect=[{"balanceScoreNum": 96}, {"balanceScoreNum": 101}]
        )
        sleep_mock = AsyncMock()
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=sleep_mock):
            self.assertTrue(await task.sign())
            bal = await task.obtain_tasks()
        self.assertEqual(bal, 101)
        self.assertEqual(sleep_mock.await_count, 2)

    async def test_woread_run_preserves_pre_reading_finish_count_and_waits_for_sync(
        self,
    ):
        task = WoreadTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.auth = AsyncMock(return_value=True)
        task.well_ticket = AsyncMock(
            return_value=("https://m.jf.10010.com/?ticket=t", "t")
        )
        task.report_reading = AsyncMock(return_value=True)
        sign_done = {
            "taskCode": JF_SIGN_TASK,
            "taskName": "每日签到",
            "finishCount": 1,
            "needCount": 1,
        }
        read_stage0 = {
            "taskCode": "read4m",
            "taskName": "登录并读听看4分钟",
            "finishCount": 0,
            "needCount": 2,
        }
        read_stage1 = {**read_stage0, "finishCount": 1}
        task_list_mock = AsyncMock(
            side_effect=[[sign_done, read_stage0], [sign_done, read_stage1]]
        )
        sleep_mock = AsyncMock()
        with (
            patch("cu_tasks.tasks.woread.asyncio.sleep", new=sleep_mock),
            patch(
                "cu_tasks.tasks.woread.JfTaskClient.init", AsyncMock(return_value=True)
            ),
            patch("cu_tasks.tasks.woread.JfTaskClient.task_list", task_list_mock),
            patch(
                "cu_tasks.tasks.woread.JfTaskClient.post",
                AsyncMock(return_value={"code": 200, "data": {"data": True}}),
            ),
            patch(
                "cu_tasks.tasks.woread.JfTaskClient.user_info",
                AsyncMock(
                    return_value={
                        "todayEarnScore": 8,
                        "availableScore": 100,
                        "allEarnScore": 50,
                    }
                ),
            ),
        ):
            await task.run()
        # 仅调 2 次 taskDetail（初始 1 次 + toFinish 后核验 1 次），且 0->1/2 阶段推进正常记成功
        self.assertEqual(task_list_mock.await_count, 2)
        self.assertIn("领积分 [登录并读听看4分钟]: 成功", messages)
        self.assertIn("结束: 共领 1 项积分", messages)
        self.assertTrue(any(c.args == (3,) for c in sleep_mock.await_args_list))

    async def test_xinjiang_parses_landing_page_activity_and_resolves_unknown_js_prize(
        self,
    ):
        task = XinjiangTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        html = (
            '<img src="../img/drawAct2026/ThemeDec2026Act/daka/daka_backup.png">'
            '<div class="dakaBtn" prize_id="daka_new_first"></div>'
            '<div class="dakaBtn" prize_id="daka_backup"></div>'
            '<script src="../pages/js/drawAct2026/ThemeDec2026Act_lhj.js?id=1"></script>'
        )
        js = "if(prizeId == 'dec_mystery_vip') {//12月限定尊享会员月卡\n return 100;\n}"

        async def fake_get_ticket(_url):
            task.last_ticket_url = (
                "https://zy100.xj169.com/touchpoint/openapi/jumpHandRoom1G?ticket=tk"
            )
            return "tk"

        async def fake_req(method, url, **kw):
            if "jumpHandRoom1G" in url:
                return {"code": 200, "headers": {}, "data": html}
            if "ThemeDec2026Act_lhj.js" in url:
                return {"code": 200, "headers": {}, "data": js}
            raise AssertionError(f"unexpected req: {url}")

        task.get_ticket = fake_get_ticket
        task.req = fake_req
        task.post = AsyncMock(
            side_effect=[
                {"code": 0, "data": {"token": "xj-tok"}},
                {"code": "SUCCESS", "data": False},
                {"code": "SUCCESS", "msg": "dec_mystery_vip", "data": ""},
                {"code": "SUCCESS", "msg": "ok"},
                {"code": -1, "msg": "fail", "data": "库存不足"},
                {"code": "SUCCESS", "msg": "daka_backup", "data": ""},
            ]
        )
        with patch("cu_tasks.tasks.xinjiang.asyncio.sleep", new=AsyncMock()):
            await task.run()
        self.assertIn("每月抽奖中奖: 12月限定尊享会员月卡", messages)
        # 打卡按按钮 prize_id 顺序（daka_new_first 失败后回退 daka_backup），不受顶部 img 干扰
        self.assertEqual(
            task.post.await_args_list[4].args,
            (
                "themeAct/draw_Dec2026Act",
                {
                    "activityId": "dakaThemeDec2026Act",
                    "prizeId": "daka_new_first",
                    "commHighFlag": "false",
                },
            ),
        )
        self.assertEqual(
            task.post.await_args_list[5].args[1]["prizeId"],
            "daka_backup",
        )

        # 重登失败时不再用空 token 重发第二次 draw
        task.post = AsyncMock(return_value={"code": -1, "data": "请刷新页面重试"})
        task.login = AsyncMock(return_value=False)
        with patch("cu_tasks.tasks.xinjiang.asyncio.sleep", new=AsyncMock()):
            await task.monthly_draw("Dec2026Act", "ThemeDec2026Act", "false")
        self.assertEqual(task.post.await_count, 1)

    async def test_uphone_winter_har_flow_claims_2508_and_draws_prize(self):
        task = UphoneTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.cp_token = "eyJ.cp"
        seen_calls = []

        har_tasks_before = [
            {
                "taskName": "月卡会员有礼",
                "taskCode": "011-2",
                "taskType": "DAILY",
                "status": "INCOMPLETE",
            },
            {
                "taskName": "体验UniClaw（养虾）",
                "taskCode": "0514-001",
                "taskType": "DAILY",
                "status": "UNCLAIMED",
            },
            {
                "taskName": "去赚积分",
                "taskCode": "2508-01",
                "taskType": "DAILY",
                "status": "UNCLAIMED",
            },
        ]
        har_tasks_after_claim = [
            har_tasks_before[0],
            har_tasks_before[1],
            {**har_tasks_before[2], "status": "OBTAINED"},
        ]
        list_responses = [
            {"code": 200, "rafflesLeftCount": 0, "taskList": har_tasks_before},
            {"code": 200, "rafflesLeftCount": 1, "taskList": har_tasks_after_claim},
            {"code": 200, "rafflesLeftCount": 0, "taskList": har_tasks_after_claim},
        ]

        async def fake_req(method, url, **kw):
            seen_calls.append((method, url, kw))
            if url.endswith("/activity-service/user/login"):
                return {
                    "code": 200,
                    "data": {"code": 200, "data": {"user_token": "usr-winter"}},
                }
            if url.endswith("/activity-service/user/task/list"):
                return {"code": 200, "data": list_responses.pop(0)}
            if url.endswith("/activity-service/user/task/raffle/get"):
                return {"code": 200, "data": {"code": 200, "msg": "操作成功"}}
            if url.endswith("/activity-service/lottery"):
                return {
                    "code": 200,
                    "data": {
                        "code": 200,
                        "msg": "恭喜中奖！",
                        "prizeName": "滴滴出行5元券",
                        "orderId": "ord-1",
                        "prizeType": 1,
                    },
                }
            raise AssertionError(f"unexpected url: {url}")

        task.req = fake_req
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            left = await task.winter_claim()
            draws = await task.winter_lottery(initial_left=left)

        self.assertEqual((left, draws), (1, 1))
        login_json = seen_calls[0][2]["json"]
        self.assertEqual(login_json["activityId"], "HD2026092100322")
        self.assertEqual(login_json["channelId"], "ST-JGQ")
        claim_json = seen_calls[2][2]["json"]
        self.assertEqual(
            claim_json, {"activityCode": "HD2026092100322", "taskCode": "2508-01"}
        )
        lottery_kw = seen_calls[4][2]
        self.assertEqual(lottery_kw["json"], {"activityCode": "HD2026092100322"})
        self.assertEqual(lottery_kw["headers"]["X-USR-TOKEN"], "usr-winter")
        self.assertIn("冬日领次数成功 2508-01 去赚积分", messages)
        self.assertIn("冬日抽奖#1 中奖: 滴滴出行5元券 type=1 order=ord-1", messages)

    async def test_uphone_winter_repeat_run_skips_when_daily_already_obtained(self):
        task = UphoneTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.cp_token = "eyJ.cp"
        task.activity_login = AsyncMock(return_value=True)
        task.raffle_get = AsyncMock()
        task.task_list = AsyncMock(
            return_value=(
                [
                    {
                        "taskCode": "011-2",
                        "taskName": "月卡会员有礼",
                        "taskType": "DAILY",
                        "status": "INCOMPLETE",
                    },
                    {
                        "taskCode": "0514-001",
                        "taskName": "体验UniClaw（养虾）",
                        "taskType": "DAILY",
                        "status": "UNCLAIMED",
                    },
                    {
                        "taskCode": "2508-01",
                        "taskName": "去赚积分",
                        "taskType": "DAILY",
                        "status": "OBTAINED",
                    },
                ],
                0,
            )
        )
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            left = await task.winter_claim()
            draws = await task.winter_lottery(initial_left=left)

        self.assertEqual((left, draws), (0, 0))
        task.raffle_get.assert_not_awaited()
        task.task_list.assert_awaited_once_with("HD2026092100322")
        self.assertIn("冬日每日任务今日已领过 rafflesLeft=0", messages)

    async def test_uphone_winter_vip_plus_fallback_and_stale_count_guard(self):
        task = UphoneTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.cp_token = "eyJ.cp"
        task.usr_token = "usr-winter"
        task.activity_login = AsyncMock(return_value=True)
        task.task_logs = AsyncMock(return_value=(True, {"code": 200}))
        task.raffle_get = AsyncMock(return_value=(True, {"code": 200}))
        task.task_list = AsyncMock(
            side_effect=[
                (
                    [
                        {
                            "taskCode": "011-2",
                            "taskName": "月卡会员有礼",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        },
                        {
                            "taskCode": "0514-001",
                            "taskName": "体验UniClaw（养虾）",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                        {
                            "taskCode": "2508-01",
                            "taskName": "去赚积分",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                    ],
                    0,
                ),
                (
                    [
                        {
                            "taskCode": "011-2",
                            "taskName": "月卡会员有礼",
                            "taskType": "DAILY",
                            "status": "OBTAINED",
                        },
                        {
                            "taskCode": "0514-001",
                            "taskName": "体验UniClaw（养虾）",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        },
                    ],
                    1,
                ),
                ([], 2),
                # 抽奖第 1 次后服务端异常未扣减次数，仍返回 2 → 触发防死循环保护
                ([], 2),
            ]
        )
        task.req = AsyncMock(
            return_value={
                "code": 200,
                "data": {"code": 200, "msg": "谢谢参与", "prizeType": 0},
            }
        )
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            left = await task.winter_claim()
            draws = await task.winter_lottery(initial_left=left)

        self.assertEqual((left, draws), (2, 1))
        task.task_logs.assert_awaited_once_with("0514-001", "Uniclaw（龙虾）")
        self.assertEqual(
            [c.args for c in task.raffle_get.await_args_list],
            [("HD2026092100322", "011-2"), ("HD2026092100322", "0514-001")],
        )
        self.assertIn("冬日抽奖次数 rafflesLeft=0→2", messages)
        self.assertIn("冬日抽奖#1: 未中奖 (谢谢参与)", messages)
        self.assertIn("冬日抽奖次数未减少(2→2)，停止抽奖", messages)

        # VIP + 回退任务双领后若末次刷新 502，预计次数应为 2（不能把 VIP 重复计入得到 3）
        messages.clear()

        async def fake_list_with_final_err(_act):
            step = fake_list_with_final_err.calls
            fake_list_with_final_err.calls += 1
            if step == 0:
                task._last_task_list_err = ""
                return (
                    [
                        {
                            "taskCode": "011-2",
                            "taskName": "月卡会员有礼",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        },
                        {
                            "taskCode": "0514-001",
                            "taskName": "体验UniClaw（养虾）",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                    ],
                    0,
                )
            if step == 1:
                task._last_task_list_err = ""
                return (
                    [
                        {
                            "taskCode": "011-2",
                            "taskName": "月卡会员有礼",
                            "taskType": "DAILY",
                            "status": "OBTAINED",
                        },
                        {
                            "taskCode": "0514-001",
                            "taskName": "体验UniClaw（养虾）",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        },
                    ],
                    1,
                )
            task._last_task_list_err = "task/list | HTTP=502"
            return [], 0

        fake_list_with_final_err.calls = 0
        task.task_list = fake_list_with_final_err
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            est_left = await task.winter_claim()
        self.assertEqual(est_left, 2)
        self.assertTrue(any("按预计次数=2继续" in m for m in messages))

        # VIP 已领 + 回退任务上报后仍未变为 UNCLAIMED：直接确认 0→1，不双计也不多发第 3 次查询
        messages.clear()
        task.task_list = AsyncMock(
            side_effect=[
                (
                    [
                        {
                            "taskCode": "011-2",
                            "taskName": "月卡会员有礼",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        },
                        {
                            "taskCode": "0514-001",
                            "taskName": "体验UniClaw（养虾）",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                    ],
                    0,
                ),
                (
                    [
                        {
                            "taskCode": "011-2",
                            "taskName": "月卡会员有礼",
                            "taskType": "DAILY",
                            "status": "OBTAINED",
                        },
                        {
                            "taskCode": "0514-001",
                            "taskName": "体验UniClaw（养虾）",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                    ],
                    1,
                ),
            ]
        )
        task._last_task_list_err = ""
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            vip_only_left = await task.winter_claim()
        self.assertEqual(vip_only_left, 1)
        self.assertEqual(task.task_list.await_count, 2)
        self.assertIn("冬日抽奖次数 rafflesLeft=0→1", messages)

    async def test_uphone_winter_monthly_obtained_does_not_block_daily_claim(self):
        task = UphoneTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.cp_token = "eyJ.cp"
        task.activity_login = AsyncMock(return_value=True)
        task.raffle_get = AsyncMock(return_value=(True, {"code": 200}))
        task.task_list = AsyncMock(
            side_effect=[
                (
                    [
                        {
                            "taskCode": "013-1",
                            "taskName": "打开通知提醒",
                            "taskType": "MONTHLY",
                            "status": "OBTAINED",
                        },
                        {
                            "taskCode": "2508-01",
                            "taskName": "去赚积分",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        },
                    ],
                    0,
                ),
                ([], 1),
            ]
        )
        left = await task.winter_claim()
        self.assertEqual(left, 1)
        task.raffle_get.assert_awaited_once_with("HD2026092100322", "2508-01")
        self.assertFalse(any("今日已领过" in m for m in messages))

    async def test_uphone_winter_prize_type_2_empty_name_and_unknown_prize_type_stop(
        self,
    ):
        from cu_tasks.runner import notify_lines

        task = UphoneTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.usr_token = "usr-winter"
        task.task_list = AsyncMock(return_value=([], 0))
        task.req = AsyncMock(
            return_value={
                "code": 200,
                "data": {
                    "code": 200,
                    "msg": "恭喜中奖！",
                    "prizeName": "",
                    "orderId": "ord-p2",
                    "prizeType": 2,
                },
            }
        )
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            draws = await task.winter_lottery(initial_left=1)
        self.assertEqual(draws, 1)
        win_line = "冬日抽奖#1 中奖: 恭喜中奖！ type=2 order=ord-p2"
        self.assertIn(win_line, messages)
        self.assertEqual(notify_lines(messages), [win_line])

        # 未知/缺失/布尔 prizeType（即便带占位 prizeName="谢谢参与"）→ 按异常停止，绝不误报中奖
        for bad_type in (None, 3, "3", -1, True, False):
            with self.subTest(bad_type=bad_type):
                messages.clear()
                task.req = AsyncMock(
                    return_value={
                        "code": 200,
                        "data": {
                            "code": 200,
                            "msg": "系统繁忙，请稍后重试",
                            "prizeName": "谢谢参与",
                            "prizeType": bad_type,
                        },
                    }
                )
                with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
                    draws2 = await task.winter_lottery(initial_left=2)
                self.assertEqual(draws2, 0)
                self.assertEqual(task.req.await_count, 1)
                self.assertTrue(any("冬日抽奖异常" in m for m in messages))
                self.assertFalse(any("中奖:" in m or "未中奖" in m for m in messages))
                self.assertEqual(len(notify_lines(messages)), 1)

    async def test_uphone_winter_task_list_and_raffle_error_paths_preserve_draws_and_diagnostics(
        self,
    ):
        task = UphoneTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.cp_token = "eyJ.cp"
        task.activity_login = AsyncMock(return_value=True)

        # A: 初始 task/list 502 → 报获取失败，不伪装成“活动未开放”
        task.req = AsyncMock(
            return_value={"code": 502, "headers": {}, "data": "Bad Gateway"}
        )
        self.assertEqual(await task.winter_claim(), 0)
        self.assertTrue(
            any("冬日任务列表获取失败" in m and "502" in m for m in messages)
        )
        self.assertFalse(any("未开放" in m for m in messages))

        # B: raffle/get 返回 10301 → 单条日志且不重复刷 task/list；C: 502 → 不打印 None
        messages.clear()
        pool_unclaimed = {
            "code": 200,
            "rafflesLeftCount": 0,
            "taskList": [
                {
                    "taskCode": "2508-01",
                    "taskName": "去赚积分",
                    "taskType": "DAILY",
                    "status": "UNCLAIMED",
                }
            ],
        }
        task.req = AsyncMock(
            side_effect=[
                {"code": 200, "data": pool_unclaimed},
                {"code": 200, "data": {"code": 10301, "msg": "今日已达上限"}},
                {"code": 200, "data": pool_unclaimed},
                {"code": 502, "headers": {}, "data": "Bad Gateway"},
            ]
        )
        self.assertEqual(await task.winter_claim(), 0)
        self.assertEqual(await task.winter_claim(), 0)
        self.assertEqual(task.req.await_count, 4)
        self.assertFalse(any("领奖受限" in m for m in messages))
        self.assertIn("冬日领次数跳过(日限) 2508-01: 今日已达上限", messages)
        self.assertTrue(
            any("冬日领次数失败 2508-01:" in m and "502" in m for m in messages)
        )
        self.assertFalse(any("None" in m for m in messages))

        # D: 领取成功后刷新 task/list 偶发 502 → 按预计次数=1 继续抽奖，不丢当日次数
        messages.clear()
        task.usr_token = "usr-winter"
        task.req = AsyncMock(
            side_effect=[
                {"code": 200, "data": pool_unclaimed},
                {"code": 200, "data": {"code": 200, "msg": "操作成功"}},
                {"code": 502, "headers": {}, "data": "Bad Gateway"},
                {
                    "code": 200,
                    "data": {
                        "code": 200,
                        "msg": "恭喜中奖！",
                        "prizeName": "联通云智手机1天卡",
                        "orderId": "ord-est",
                        "prizeType": 1,
                    },
                },
                {
                    "code": 200,
                    "data": {"code": 200, "rafflesLeftCount": 0, "taskList": []},
                },
            ]
        )
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            left = await task.winter_claim()
            draws = await task.winter_lottery(initial_left=left)
        self.assertEqual((left, draws), (1, 1))
        self.assertIn(
            "冬日抽奖#1 中奖: 联通云智手机1天卡 type=1 order=ord-est", messages
        )

    async def test_uphone_run_continues_to_winter_and_secondary_fallback_when_sign_login_fails(
        self,
    ):
        task = UphoneTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.sso_login = AsyncMock(return_value=True)
        task.checkin = AsyncMock()

        async def fake_login(act_id, channel_id="ST-Jingang002"):
            if act_id == "HD2026092100322" and channel_id == "ST-JGQ":
                task.usr_token = "usr-winter"
                return True
            return False

        task.activity_login = AsyncMock(side_effect=fake_login)
        task.task_logs = AsyncMock(return_value=(True, {"code": 200}))
        task.raffle_get = AsyncMock(return_value=(True, {"code": 200}))
        task.task_list = AsyncMock(
            side_effect=[
                (
                    [
                        {
                            "taskCode": "1001-12",
                            "taskName": "体验云存储",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                        {
                            "taskCode": "2508-01",
                            "taskName": "去赚积分",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                    ],
                    0,
                ),
                (
                    [
                        {
                            "taskCode": "1001-12",
                            "taskName": "体验云存储",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        }
                    ],
                    0,
                ),
                ([], 1),
                ([], 0),
            ]
        )
        task.req = AsyncMock(
            return_value={
                "code": 200,
                "data": {"code": 200, "msg": "谢谢参与", "prizeType": 0},
            }
        )
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            await task.run()

        task.task_logs.assert_awaited_once_with("1001-12", "云存储")
        task.raffle_get.assert_awaited_once_with("HD2026092100322", "1001-12")
        self.assertIn("冬日抽奖#1: 未中奖 (谢谢参与)", messages)
        self.assertEqual(messages[-1], "结束")

    async def test_uphone_masks_embedded_mobile_in_order_id_while_preserving_checkin_post_payload(
        self,
    ):
        import cu_tasks.tasks.uphone as uphone_mod
        from cu_tasks import runner

        for obsolete in (
            "ACT_SUMMER",
            "SUMMER_CLAIM_TASK",
            "SUMMER_MAX_DRAWS",
            "summer_claim_one",
            "summer_lottery",
        ):
            self.assertFalse(hasattr(uphone_mod, obsolete))
            self.assertFalse(hasattr(UphoneTask, obsolete))

        self.account.mobile = "16600000199"
        task = UphoneTask(self.account)
        self.addAsyncCleanup(task.close)
        task.cp_token = "eyJ.cp"
        task.usr_token = "usr-winter"
        task.task_list = AsyncMock(return_value=([], 0))
        live_order = "1660000019917949750000000002"
        har_order = "1660000888817949740000000001"
        task.req = AsyncMock(
            side_effect=[
                {
                    "code": 200,
                    "data": {
                        "code": 200,
                        "msg": "恭喜中奖！",
                        "prizeName": "滴滴出行5元券",
                        "orderId": live_order,
                        "prizeType": 1,
                    },
                },
                {
                    "code": 200,
                    "data": {
                        "code": 200,
                        "msg": "恭喜中奖！",
                        "prizeName": "滴滴出行8元券",
                        "orderId": har_order,
                        "prizeType": 1,
                    },
                },
            ]
        )
        with (
            patch("builtins.print") as mock_print,
            patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()),
        ):
            self.assertEqual(await task.winter_lottery(initial_left=1), 1)
            self.assertEqual(await task.winter_lottery(initial_left=1), 1)
            # checkin_raffle 传给服务端的 activityOrderid 必须保持原始值，仅日志脱敏
            task.checkin_post = AsyncMock(return_value={"code": 200})
            self.assertTrue(await task.checkin_raffle(live_order))

        task.checkin_post.assert_awaited_once_with(
            "activity/raffleSignIn",
            {"activityOrderid": live_order, "account": "", "accountType": ""},
        )
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        combined = (
            "\n".join(task.lines + runner.notify_lines(task.lines)) + "\n" + printed
        )
        self.assertNotIn("16600000199", combined)
        self.assertNotIn("16600008888", combined)
        self.assertIn("166****019917949750000000002", combined)
        self.assertIn("166****888817949740000000001", combined)
        self.assertEqual(await task.winter_lottery(max_draws=0, initial_left=None), 0)

        # usr_token 为空但传入 initial_left>0 时也应先登录获取 usr_token
        task.usr_token = ""
        task.activity_login = AsyncMock(return_value=False)
        self.assertEqual(await task.winter_lottery(initial_left=1), 0)
        task.activity_login.assert_awaited_once_with("HD2026092100322", "ST-JGQ")

    async def test_uphone_winter_fallback_continues_past_stalled_log_and_handles_vip_lag(
        self,
    ):
        task = UphoneTask(self.account)
        self.addAsyncCleanup(task.close)
        messages = []
        task.tlog = messages.append
        task.cp_token = "eyJ.cp"
        task.activity_login = AsyncMock(return_value=True)
        task.task_logs = AsyncMock(return_value=(True, {"code": 200}))
        task.raffle_get = AsyncMock(return_value=(True, {"code": 200}))

        # A: 首个回退任务上报返回 200 但状态仍为 INCOMPLETE 时，继续尝试后续回退任务
        task.task_list = AsyncMock(
            side_effect=[
                (
                    [
                        {
                            "taskCode": "0514-001",
                            "taskName": "养虾",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                        {
                            "taskCode": "1001-12",
                            "taskName": "云存储",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                    ],
                    0,
                ),
                (
                    [
                        {
                            "taskCode": "0514-001",
                            "taskName": "养虾",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                        {
                            "taskCode": "1001-12",
                            "taskName": "云存储",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                    ],
                    0,
                ),
                (
                    [
                        {
                            "taskCode": "0514-001",
                            "taskName": "养虾",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                        {
                            "taskCode": "1001-12",
                            "taskName": "云存储",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        },
                    ],
                    0,
                ),
                ([], 1),
            ]
        )
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            left_a = await task.winter_claim()
        self.assertEqual(left_a, 1)
        self.assertEqual(
            [c.args for c in task.task_logs.await_args_list],
            [("0514-001", "Uniclaw（龙虾）"), ("1001-12", "云存储")],
        )
        task.raffle_get.assert_awaited_once_with("HD2026092100322", "1001-12")

        # B: VIP 011-2 已领但回退刷新时异步入账延迟(left_fb=0)，仍补刷新拿到 1 次，不丢 VIP 次数
        messages.clear()
        task.task_logs.reset_mock()
        task.raffle_get.reset_mock()
        task.task_list = AsyncMock(
            side_effect=[
                (
                    [
                        {
                            "taskCode": "011-2",
                            "taskName": "月卡会员有礼",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        },
                        {
                            "taskCode": "0514-001",
                            "taskName": "养虾",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                    ],
                    0,
                ),
                (
                    [
                        {
                            "taskCode": "011-2",
                            "taskName": "月卡会员有礼",
                            "taskType": "DAILY",
                            "status": "OBTAINED",
                        },
                        {
                            "taskCode": "0514-001",
                            "taskName": "养虾",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                    ],
                    0,
                ),
                ([], 1),
            ]
        )
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            left_b = await task.winter_claim()
        self.assertEqual(left_b, 1)
        self.assertIn("冬日无可领任务 rafflesLeft=1（未达条件，跳过）", messages)
        self.assertIn("冬日抽奖次数 rafflesLeft=0→1", messages)

        # C: VIP 011-2 已领 + 回退刷新时异步入账延迟(left_fb=0)但回退任务可领并领成功，末次 502 仍预计=2（不漏算为 1）
        messages.clear()

        async def fake_lag_then_err(_act):
            step = fake_lag_then_err.calls
            fake_lag_then_err.calls += 1
            if step == 0:
                task._last_task_list_err = ""
                return (
                    [
                        {
                            "taskCode": "011-2",
                            "taskName": "月卡会员有礼",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        },
                        {
                            "taskCode": "0514-001",
                            "taskName": "养虾",
                            "taskType": "DAILY",
                            "status": "INCOMPLETE",
                        },
                    ],
                    0,
                )
            if step == 1:
                task._last_task_list_err = ""
                return (
                    [
                        {
                            "taskCode": "011-2",
                            "taskName": "月卡会员有礼",
                            "taskType": "DAILY",
                            "status": "OBTAINED",
                        },
                        {
                            "taskCode": "0514-001",
                            "taskName": "养虾",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        },
                    ],
                    0,
                )
            task._last_task_list_err = "task/list | HTTP=502"
            return [], 0

        fake_lag_then_err.calls = 0
        task.task_list = fake_lag_then_err
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            left_c = await task.winter_claim()
        self.assertEqual(left_c, 2)
        self.assertTrue(any("按预计次数=2继续" in m for m in messages))

        # D: 领奖后首次刷新仍为 0（入账延迟），等待 0.5s 二次刷新落库为 1
        messages.clear()
        task._last_task_list_err = ""
        task.task_list = AsyncMock(
            side_effect=[
                (
                    [
                        {
                            "taskCode": "2508-01",
                            "taskName": "去赚积分",
                            "taskType": "DAILY",
                            "status": "UNCLAIMED",
                        }
                    ],
                    0,
                ),
                ([], 0),
                ([], 1),
            ]
        )
        with patch("cu_tasks.tasks.uphone.asyncio.sleep", new=AsyncMock()):
            left_d = await task.winter_claim()
        self.assertEqual(left_d, 1)
        self.assertIn("冬日抽奖次数 rafflesLeft=0→1", messages)


if __name__ == "__main__":
    unittest.main()
