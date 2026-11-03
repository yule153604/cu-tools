"""脱敏协议响应与离线 HTTP 模拟，不读取 HAR，也不请求真实账号。"""

import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from cu import Unicom
from cu_tasks.welfare import WelfareConfig


def success(data):
    return {"status": "success", "code": "0", "msg": "成功", "resdata": data}


ACTIVITY = {
    "acState": "2",
    "luckType": "0",
    "consumptionIntegral": "",
    "isTarget": "true",
}


class WelfareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.wait = patch("cu_tasks.welfare.RESULT_WAIT_SECONDS", 0)
        self.wait.start()
        polling = patch("cu_tasks.welfare.RESULT_POLL_SECONDS", 0)
        polling.start()
        self.addCleanup(polling.stop)
        interval = patch("cu_tasks.welfare.DRAW_INTERVAL_SECONDS", 0)
        interval.start()
        self.addCleanup(interval.stop)
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.wait.stop)
        self.account = Unicom("test-token")
        await self.account.close()
        self.calls = []
        self.responses = []
        self.messages = []
        self.account.tlog = self.messages.append
        self.account.client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle)
        )
        self.account.client.cookies.set(
            "ecs_token", "test-session", domain=".10010.com"
        )
        self.addAsyncCleanup(self.account.close)

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
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def initial(self, count="3", activity=None):
        self.responses = [
            ("findActivity", success(ACTIVITY if activity is None else activity)),
            ("findLotteryCount", success({"drawCount": count})),
        ]

    async def test_har_success_queries_result_with_same_sequence(self):
        self.initial("2")
        for remaining in (1, 0):
            self.responses.extend(
                [
                    ("luckyDraw", success("")),
                    ("findWinInfo", success({"isWin": True, "prizeName": "8积分"})),
                    ("findLotteryCount", success({"drawCount": str(remaining)})),
                ]
            )
        await self.account.welfare_task()
        self.assertFalse(self.responses)
        first, second = self.calls[2][1], self.calls[5][1]
        self.assertEqual(first, self.calls[3][1])
        self.assertEqual(second, self.calls[6][1])
        self.assertNotEqual(first["reqSeq"], second["reqSeq"])
        self.assertEqual(first["imei"], self.account.uuid.upper())
        self.assertEqual(first["acId"], "AC20260920094443")
        self.assertEqual(first["from"], "ZXGS97000020362")
        self.assertTrue(any("8积分" in m for m in self.messages))

    async def test_pending_result_polls_same_sequence_until_prize(self):
        self.initial("1")
        self.responses.extend(
            [
                ("luckyDraw", success("")),
                ("findWinInfo", success({"isWin": False})),
                ("findWinInfo", success({"isWin": "false"})),
                ("findWinInfo", success({"isWin": True, "prizeName": "8积分"})),
                ("findLotteryCount", success({"drawCount": "0"})),
            ]
        )
        await self.account.welfare_task()
        self.assertFalse(self.responses)
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 1)
        self.assertTrue(all(p == self.calls[2][1] for _, p in self.calls[3:]))
        self.assertTrue(any("8积分" in m for m in self.messages))

    async def test_har_no_prize_polls_four_times_then_uses_remaining_count(self):
        self.initial("2")
        for remaining in (1, 0):
            self.responses.append(("luckyDraw", success("")))
            self.responses.extend(
                [("findWinInfo", success({"isWin": False, "prizeName": None}))] * 4
            )
            self.responses.append(
                ("findLotteryCount", success({"drawCount": str(remaining)}))
            )
        await self.account.welfare_task()
        self.assertFalse(self.responses)
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 2)
        self.assertEqual([a for a, _ in self.calls].count("findWinInfo"), 8)
        first, second = self.calls[2][1], self.calls[8][1]
        self.assertNotEqual(first["reqSeq"], second["reqSeq"])
        self.assertTrue(all(p == first for _, p in self.calls[3:8]))
        self.assertTrue(all(p == second for _, p in self.calls[9:]))

    async def test_poll_failure_stops_without_another_draw(self):
        self.initial("2")
        self.responses.extend(
            [
                ("luckyDraw", success("")),
                ("findWinInfo", success({"isWin": False})),
                ("findWinInfo", httpx.Response(503, text="unavailable")),
            ]
        )
        with self.assertRaises(ValueError):
            await self.account.welfare_task()
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 1)

    async def test_timeout_only_polls_result_and_never_resubmits(self):
        self.initial("3")
        self.responses.extend(
            [("luckyDraw", httpx.ReadTimeout("simulated timeout"))]
            + [("findWinInfo", success({"isWin": False}))] * 4
        )
        await self.account.welfare_task()
        self.assertFalse(self.responses)
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 1)
        self.assertEqual(self.calls[-1][0], "findWinInfo")

    async def test_zero_count_does_not_draw(self):
        self.initial("0")
        await self.account.welfare_task()
        self.assertEqual(len(self.calls), 2)

    async def test_unopened_and_non_target_stop_before_count(self):
        for overrides in ({"acState": "1"}, {"isTarget": "false"}, {"acState": None}):
            self.responses = [("findActivity", success({**ACTIVITY, **overrides}))]
            await self.account.welfare_task()
            self.assertFalse(self.responses)
        self.assertTrue(all(action == "findActivity" for action, _ in self.calls))

    async def test_har_busy_status_success_is_not_business_success(self):
        self.initial()
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
                ("findWinInfo", success({"isWin": True, "prizeName": "8积分"})),
                ("findLotteryCount", success({"drawCount": "3"})),
            ]
        )
        await self.account.welfare_task()
        self.assertEqual([action for action, _ in self.calls].count("luckyDraw"), 1)
        self.assertEqual(self.calls[-1][1], self.calls[-2][1])
        self.assertTrue(any("1129" in message for message in self.messages))

    async def test_no_prize_code_is_normal_and_continues_when_consumed(self):
        self.initial("2")
        self.responses.extend(
            [
                ("luckyDraw", {"code": "1129", "msg": "亲，活动太火爆了请重试"}),
                (
                    "findWinInfo",
                    {"code": "1123", "msg": "很遗憾，大奖与您擦肩而过", "resdata": ""},
                ),
                ("findLotteryCount", success({"drawCount": "1"})),
                ("luckyDraw", success("")),
                ("findWinInfo", {"code": "1123", "resdata": None}),
                ("findLotteryCount", success({"drawCount": "0"})),
            ]
        )
        await self.account.welfare_task()
        self.assertFalse(self.responses)
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 2)
        self.assertFalse(any("异常" in message for message in self.messages))
        self.assertNotEqual(self.calls[2][1]["reqSeq"], self.calls[5][1]["reqSeq"])

    async def test_special_codes_are_not_general_success(self):
        for code in ("1123", "1129"):
            self.responses = [("findActivity", {"code": code, "resdata": ACTIVITY})]
            with self.assertRaises(ValueError):
                await self.account.welfare_task()

    async def test_timeout_reconciles_without_resubmitting(self):
        self.initial()
        self.responses.extend(
            [
                ("luckyDraw", httpx.ReadTimeout("simulated timeout")),
                ("findWinInfo", success({"isWin": True, "prizeName": "测试奖品"})),
            ]
        )
        await self.account.welfare_task()
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 1)
        self.assertTrue(any("测试奖品" in m for m in self.messages))

    async def test_stale_count_stops(self):
        self.initial()
        self.responses.extend(
            [
                ("luckyDraw", success("")),
                ("findWinInfo", success({"isWin": True, "prizeName": "8积分"})),
                ("findLotteryCount", success({"drawCount": "3"})),
            ]
        )
        await self.account.welfare_task()
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(any("次数未减少" in m for m in self.messages))

    async def test_invalid_counts_never_submit(self):
        for count in (None, True, -1, "abc", 1.5, {}, ""):
            self.initial(count)
            with self.assertRaises(ValueError):
                await self.account.welfare_task()
        self.assertNotIn("luckyDraw", [a for a, _ in self.calls])

    async def test_invalid_response_fails_before_draw(self):
        for response in (
            success([]),
            success(None),
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, text="{broken"),
        ):
            self.responses = [("findActivity", response)]
            with self.assertRaises(ValueError):
                await self.account.welfare_task()

    async def test_result_query_failure_stops_further_draws(self):
        self.initial()
        self.responses.extend(
            [
                ("luckyDraw", success("")),
                ("findWinInfo", success({})),
            ]
        )
        with self.assertRaises(ValueError):
            await self.account.welfare_task()
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 1)

    async def test_draw_limit_and_activity_override(self):
        os.environ.update(
            UNICOM_WELFARE_MAX_DRAWS="1", UNICOM_WELFARE_ACTIVITY_ID="next-period"
        )
        self.initial("999")
        self.responses.extend(
            [
                ("luckyDraw", success("")),
                ("findWinInfo", success({"isWin": True, "prizeName": "8积分"})),
                ("findLotteryCount", success({"drawCount": "998"})),
            ]
        )
        await self.account.welfare_task()
        self.assertTrue(all(p["acId"] == "next-period" for _, p in self.calls))
        self.assertEqual([a for a, _ in self.calls].count("luckyDraw"), 1)

    async def test_zero_limit_makes_no_requests(self):
        os.environ["UNICOM_WELFARE_MAX_DRAWS"] = "0"
        await self.account.welfare_task()
        self.assertEqual(self.calls, [])


class ConfigTests(unittest.TestCase):
    def test_invalid_limits_and_empty_ids(self):
        for name, value in (
            ("UNICOM_WELFARE_MAX_DRAWS", "-1"),
            ("UNICOM_WELFARE_MAX_DRAWS", "11"),
            ("UNICOM_WELFARE_MAX_DRAWS", ""),
            ("UNICOM_WELFARE_ACTIVITY_ID", " "),
            ("UNICOM_WELFARE_FROM", ""),
        ):
            with (
                self.subTest(name=name, value=value),
                patch.dict(os.environ, {name: value}, clear=True),
            ):
                with self.assertRaises(ValueError):
                    WelfareConfig.from_env()
