"""2026-11-16 好运开转（老虎机）脱敏协议响应与离线 HTTP 模拟，不读取 HAR。"""

import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from cu_tasks.config import Settings
from cu_tasks.registry import TASKS, select_tasks
from cu_tasks.session import Account
from cu_tasks.tasks.slot import SlotTask


def success(data):
    return {"status": "success", "code": "0", "msg": "成功", "resdata": data}


ACTIVITY = {
    "acState": "2",
    "luckType": "1",
    "consumptionIntegral": "10",
    "isTarget": "true",
}


class SlotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for attr in (
            "RESULT_WAIT_SECONDS",
            "RESULT_POLL_SECONDS",
            "DRAW_INTERVAL_SECONDS",
        ):
            p = patch(f"cu_tasks.tasks.welfare.{attr}", 0)
            p.start()
            self.addCleanup(p.stop)
        self.account = Account("test-token", transport=httpx.MockTransport(self.handle))
        self.account.client.cookies.set(
            "ecs_token", "test-session", domain=".10010.com"
        )
        self.addAsyncCleanup(self.account.close)
        self.calls = []
        self.responses = []
        self.messages = []

    async def run_task(self, **settings):
        task = SlotTask(self.account, Settings(**settings))
        task.tlog = self.messages.append
        try:
            await task.run()
        finally:
            await task.close()

    def handle(self, request):
        action = request.url.path.split("/")[-2]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["origin"], "https://img.client.10010.com")
        self.assertIn("ecs_token=test-session", request.headers["cookie"])
        form = parse_qs(request.content.decode())
        self.assertEqual(set(form), {"params"})
        payload = json.loads(form["params"][0])
        self.calls.append((action, payload))
        self.assertTrue(self.responses, f"意外请求: {action}")
        expected, response = self.responses.pop(0)
        self.assertEqual(action, expected)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def initial(self, count="3", activity=None):
        self.responses = [
            ("findActivity", success(ACTIVITY if activity is None else activity)),
            ("findLotteryCount", success({"drawCount": count})),
        ]

    async def test_har_slot_flow_wins_points(self):
        self.initial("3")
        self.responses.extend(
            [
                ("luckyDraw", success("")),
                ("findWinInfo", success({"isWin": False, "prizeName": None})),
                ("findWinInfo", success({"isWin": False, "prizeName": None})),
                ("findWinInfo", success({"isWin": False, "prizeName": None})),
                ("findWinInfo", success({"isWin": False, "prizeName": None})),
                ("findLotteryCount", success({"drawCount": "2"})),
                ("luckyDraw", success("")),
                ("findWinInfo", success({"isWin": True, "prizeName": "600积分"})),
                ("findLotteryCount", success({"drawCount": "1"})),
                ("luckyDraw", success("")),
                ("findWinInfo", success({"isWin": True, "prizeName": "100积分"})),
                ("findLotteryCount", success({"drawCount": "0"})),
            ]
        )
        await self.run_task()
        self.assertFalse(self.responses)
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 3)
        for _, payload in self.calls:
            self.assertEqual(payload["acId"], "AC20261111142835")
            self.assertEqual(payload["from"], "ZXGS97000020415")
        seqs = [p["reqSeq"] for a, p in self.calls if a == "luckyDraw"]
        self.assertEqual(len(set(seqs)), 3)
        self.assertTrue(any("600积分" in m for m in self.messages))
        self.assertTrue(any("100积分" in m for m in self.messages))

    async def test_har_busy_1129_without_consumption_stops(self):
        self.initial("1")
        self.responses.extend(
            [
                (
                    "luckyDraw",
                    {
                        "status": "success",
                        "code": "1129",
                        "msg": "亲，活动太火爆了请重试",
                        "resdata": "",
                    },
                ),
                ("findWinInfo", success({"isWin": False})),
                ("findWinInfo", success({"isWin": False})),
                ("findWinInfo", success({"isWin": False})),
                ("findWinInfo", success({"isWin": False})),
                ("findLotteryCount", success({"drawCount": "1"})),
            ]
        )
        await self.run_task()
        self.assertFalse(self.responses)
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 1)
        self.assertTrue(any("次数未减少" in m for m in self.messages))

    async def test_insufficient_points_1126_stops_without_polling(self):
        self.initial("3")
        self.responses.append(
            ("luckyDraw", {"status": "success", "code": "1126", "msg": "积分不足"})
        )
        await self.run_task()
        self.assertFalse(self.responses)
        self.assertEqual(
            [a for a, _ in self.calls],
            [
                "findActivity",
                "findLotteryCount",
                "luckyDraw",
            ],
        )
        self.assertTrue(any("积分不足" in m for m in self.messages))

    async def test_non_target_1124_on_draw_stops_without_polling(self):
        self.initial("3")
        self.responses.append(
            ("luckyDraw", {"status": "success", "code": "1124", "msg": "非目标用户"})
        )
        await self.run_task()
        self.assertFalse(self.responses)
        self.assertEqual(
            [a for a, _ in self.calls],
            [
                "findActivity",
                "findLotteryCount",
                "luckyDraw",
            ],
        )
        self.assertTrue(any("不满足活动参与条件" in m for m in self.messages))

    async def test_direct_no_prize_1123_checks_count_without_polling(self):
        self.initial("2")
        self.responses.extend(
            [
                ("luckyDraw", {"status": "success", "code": "1123", "msg": "未中奖"}),
                ("findLotteryCount", success({"drawCount": "1"})),
                ("luckyDraw", success("")),
                ("findWinInfo", success({"isWin": True, "prizeName": "100积分"})),
                ("findLotteryCount", success({"drawCount": "0"})),
            ]
        )
        await self.run_task()
        self.assertFalse(self.responses)
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 2)
        self.assertEqual([a for a, _ in self.calls].count("findWinInfo"), 1)
        self.assertTrue(any("未中奖" in m for m in self.messages))
        self.assertTrue(any("100积分" in m for m in self.messages))

    async def test_cost_above_limit_skips_before_count(self):
        for cost, limit in (("50", 10), ("10", 0)):
            with self.subTest(cost=cost, limit=limit):
                self.calls.clear()
                self.messages.clear()
                self.responses = [
                    ("findActivity", success({**ACTIVITY, "consumptionIntegral": cost}))
                ]
                await self.run_task(slot_max_cost=limit)
                self.assertFalse(self.responses)
                self.assertEqual([a for a, _ in self.calls], ["findActivity"])
                self.assertTrue(any("超过上限" in m for m in self.messages))

    async def test_missing_cost_when_paid_skips_before_count(self):
        self.responses = [
            ("findActivity", success({**ACTIVITY, "consumptionIntegral": ""}))
        ]
        await self.run_task()
        self.assertFalse(self.responses)
        self.assertEqual([a for a, _ in self.calls], ["findActivity"])
        self.assertTrue(any("配置缺失" in m for m in self.messages))

    async def test_invalid_cost_fails_before_draw(self):
        self.responses = [
            ("findActivity", success({**ACTIVITY, "consumptionIntegral": "abc"}))
        ]
        with self.assertRaises(ValueError):
            await self.run_task()
        self.assertNotIn("luckyDraw", [a for a, _ in self.calls])

    async def test_zero_limit_makes_no_requests(self):
        await self.run_task(slot_max_draws=0)
        self.assertEqual(self.calls, [])


class SlotConfigAndRegistryTests(unittest.TestCase):
    def test_slot_is_registered_and_selectable(self):
        self.assertIn(SlotTask, TASKS)
        self.assertEqual(select_tasks("slot"), (SlotTask,))

    def test_invalid_slot_settings_raise(self):
        for name, value in (
            ("UNICOM_SLOT_MAX_DRAWS", "-1"),
            ("UNICOM_SLOT_MAX_DRAWS", "11"),
            ("UNICOM_SLOT_MAX_DRAWS", ""),
            ("UNICOM_SLOT_MAX_COST", "-1"),
            ("UNICOM_SLOT_MAX_COST", "x"),
            ("UNICOM_SLOT_ACTIVITY_ID", " "),
            ("UNICOM_SLOT_FROM", ""),
        ):
            with (
                self.subTest(name=name, value=value),
                patch.dict(os.environ, {name: value}, clear=True),
            ):
                with self.assertRaises(ValueError):
                    Settings.from_env()
