"""2026-10-31 商都小程序协议与领取结果回归；不访问真实账号。"""

import copy
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from cu_tasks.session import Account
from cu_tasks.tasks.shangdu import APP_ID as SHANGDU_APP_ID
from cu_tasks.tasks.shangdu import CHANNEL as SHANGDU_CHANNEL
from cu_tasks.tasks.shangdu import ShangduTask

# 新 HAR 的领取响应，仅包含 code/msg/SESSION_EXHAUSTED，不含账号或凭证。
EXHAUSTED_CIPHER = '"048941998d2c90976add294e26bc7fe06573579fd2e45d23cbce96d50e483eeb75fc05f04330e3e38a69018b199e4180cda852ef2395784749d8390f93a8a0262e2c0a62dee55fdb0a27a9ddcaa8905bc94cf13787601ff9decb67651a41067cb742605d887a76044c2d0573aadb599f7b88008d475e7cd0dce153182fd0cb4142d81cb0e18eeffc7c17607d22e838abd24c13901680e732d40771c85541bda80159e25e3fda6da8264b38d9a2161eaf563b89f341e07b399979363ea58ec38733"'


def ok(data=None):
    return {"code": 200, "data": {"code": 200, "msg": "操作成功", "data": data}}


def activity(sign=1, remain=0, disabled=True, nodes=None, festival=False, count=None):
    return {
        "isTargetUser": True,
        "signInInfo": {
            "status": sign,
            "signInCount": (14 if sign == 1 else 6) if count is None else count,
            "rewardNodes": nodes or [],
            "signInRewardSession": {
                "status": "NOT_STARTED" if disabled else "ISSUING",
                "disabled": disabled,
                "displayText": "第3场倒计时" if disabled else "本场发放中",
            },
        },
        "lotteryInfo": {"remainCount": remain, "isFestival": festival},
    }


def node(day=7, status="CLAIMABLE"):
    return {"day": day, "status": status}


class ShangduTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        account = Account("test-online-token")
        account.ecs_token = "test-current-ecs"
        account.province = "河南"
        self.addAsyncCleanup(account.close)
        self.task = ShangduTask(account)
        self.addAsyncCleanup(self.task.close)
        self.messages = []
        self.task.tlog = self.messages.append
        sleep = patch("cu_tasks.tasks.shangdu.asyncio.sleep", new=AsyncMock())
        sleep.start()
        self.addCleanup(sleep.stop)
        wait_limit = patch("cu_tasks.tasks.shangdu.CLAIM_WAIT_MAX_SECONDS", 0)
        wait_limit.start()
        self.addCleanup(wait_limit.stop)
        self.calls = []

    def mock_api(self, responses):
        self.responses = list(responses)

        async def call(method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            self.assertTrue(self.responses, f"意外商都请求: {method} {path}")
            expected_method, expected_path, response = self.responses.pop(0)
            self.assertEqual((method, path), (expected_method, expected_path))
            if isinstance(response, Exception):
                raise response
            return copy.deepcopy(response)

        self.task.api = AsyncMock(side_effect=call)
        self.task.login = AsyncMock(return_value=True)
        self.task.click_log = AsyncMock()

    def task_responses(self, initial, refreshed=None):
        return [
            ("GET", "/monthlyRecharge/init", ok(initial)),
            ("GET", "/monthlyRecharge/rollingText", ok([])),
            ("GET", "/monthlyRecharge/init", ok(refreshed or initial)),
        ]

    async def test_applet_login_uses_current_ticket_and_authorization(self):
        self.task.req = AsyncMock(
            side_effect=[
                {"code": 200, "data": {"rsp_code": "0000", "ticket": "test-ticket"}},
                {"code": 200, "data": {"code": 200, "data": {"token": "new-token"}}},
                {"code": 200, "data": {"code": 200, "data": activity()}},
            ]
        )
        self.task.access_token = "stale-token"
        self.assertTrue(await self.task.login())
        await self.task.fetch_state()
        ticket, login, query = self.task.req.await_args_list
        self.assertEqual(
            ticket.kwargs["params"],
            {"appId": SHANGDU_APP_ID, "token": "test-current-ecs"},
        )
        self.assertTrue(login.args[1].endswith("/auth/loginAppAppletFree"))
        self.assertEqual(
            login.kwargs["json"], {"ticket": "test-ticket", "channel": SHANGDU_CHANNEL}
        )
        self.assertNotIn("Authorization", login.kwargs["headers"])
        self.assertEqual(login.kwargs["headers"]["X-Client-Type"], "APPLET")
        self.assertEqual(login.kwargs["headers"]["Content-Type"], "application/json")
        self.assertNotIn("Referer", login.kwargs["headers"])
        self.assertNotIn("Origin", login.kwargs["headers"])
        self.assertEqual(query.kwargs["headers"]["Authorization"], "new-token")

    async def test_invalid_ticket_never_logs_in_or_reuses_stale_token(self):
        for response in (
            {"code": 503, "data": {}},
            {"code": 200, "data": {"rsp_code": "0001", "ticket": "bad"}},
            {"code": 200, "data": {"rsp_code": "0000", "ticket": ""}},
            {"code": 200, "data": None},
        ):
            self.task.access_token = "stale-token"
            self.task.req = AsyncMock(return_value=response)
            self.assertFalse(await self.task.login())
            self.assertEqual(self.task.access_token, "")
            self.assertEqual(self.task.req.await_count, 1)

    async def test_login_missing_token_is_failure(self):
        self.task.req = AsyncMock(
            side_effect=[
                {"code": 200, "data": {"rsp_code": "0000", "ticket": "test-ticket"}},
                {"code": 200, "data": {"code": 200, "data": {}}},
            ]
        )
        self.assertFalse(await self.task.login())
        self.assertEqual(self.task.access_token, "")

    async def test_har_encrypted_exhaustion_is_not_claim_success(self):
        self.task.req = AsyncMock(return_value={"code": 200, "data": EXHAUSTED_CIPHER})
        response = await self.task.api("POST", "/monthlyRecharge/signIn/reward/7/claim")
        self.assertEqual(self.task.req.await_args.kwargs["json"], {})
        decoded = response["data"]
        self.assertEqual(decoded["data"]["status"], "SESSION_EXHAUSTED")
        self.mock_api(
            [
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    {"code": 200, "data": decoded},
                ),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/7/claim",
                    {"code": 200, "data": decoded},
                ),
            ]
        )
        await self.task.claim_rewards(
            activity(disabled=False, nodes=[node(7), node(14)])
        )
        self.assertFalse(self.responses)
        self.assertTrue(any("本场已领完" in m for m in self.messages))
        self.assertFalse(any("领取成功" in m for m in self.messages))

    async def test_disabled_or_missing_session_never_claims(self):
        self.mock_api([])
        for disabled in (True, "false", None):
            await self.task.claim_rewards(activity(disabled=disabled, nodes=[node()]))
        missing = activity(nodes=[node()])
        missing["signInInfo"].pop("signInRewardSession")
        await self.task.claim_rewards(missing)
        self.assertEqual(self.calls, [])

    async def test_claimed_monthly_two_and_five_yuan_nodes_are_skipped(self):
        self.mock_api([])
        await self.task.claim_rewards(
            activity(disabled=False, nodes=[node(7, "CLAIMED"), node(14, "CLAIMED")])
        )
        self.assertEqual(self.calls, [])

    async def test_claimed_two_yuan_node_does_not_block_five_yuan_node(self):
        initial = activity(disabled=False, nodes=[node(7, "CLAIMED"), node(14)])
        claimed = activity(nodes=[node(7, "CLAIMED"), node(14, "CLAIMED")])
        self.mock_api(
            [
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "CLAIMED"}),
                ),
                ("GET", "/monthlyRecharge/init", ok(claimed)),
            ]
        )
        await self.task.claim_rewards(initial)
        self.assertFalse(self.responses)

    async def test_claim_only_never_signs_or_draws(self):
        self.mock_api(
            [
                ("GET", "/monthlyRecharge/init", ok(activity(sign=0, remain=3))),
            ]
        )
        await self.task.execute(claim_only=True)
        self.assertFalse(self.responses)
        self.assertEqual(len(self.calls), 1)

    async def test_claim_requires_read_back_claimed_state(self):
        self.mock_api(
            [
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/7/claim",
                    ok({"status": "CLAIMED"}),
                ),
                (
                    "GET",
                    "/monthlyRecharge/init",
                    ok(activity(nodes=[node(status="CLAIMED")])),
                ),
            ]
        )
        await self.task.claim_rewards(activity(disabled=False, nodes=[node()]))
        self.assertFalse(self.responses)
        self.assertTrue(any("已读回 CLAIMED" in m for m in self.messages))

    async def test_unknown_receipt_does_not_claim_next_node(self):
        current = activity(disabled=False, nodes=[node(7), node(14)])
        self.mock_api(
            [
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "UNKNOWN"}),
                ),
                ("GET", "/monthlyRecharge/init", ok(current)),
            ]
        )
        await self.task.claim_rewards(current)
        self.assertFalse(self.responses)
        self.assertFalse(any("领取成功" in m for m in self.messages))
        self.assertTrue(any("不重试" in m for m in self.messages))

    async def test_refresh_failure_stops_before_draw(self):
        self.mock_api(
            [
                ("GET", "/monthlyRecharge/init", ok(activity(remain=3))),
                ("GET", "/monthlyRecharge/rollingText", ok([])),
                ("GET", "/monthlyRecharge/init", {"code": 503, "data": None}),
            ]
        )
        with self.assertRaises(ValueError):
            await self.task.execute()
        self.assertFalse(any(method == "POST" for method, _, _ in self.calls))

    async def test_newly_signed_nodes_are_used_instead_of_stale_nodes(self):
        fresh = activity(disabled=False, nodes=[node()])
        claimed = activity(nodes=[node(status="CLAIMED")])
        self.mock_api(
            [
                ("GET", "/monthlyRecharge/init", ok(activity(sign=0))),
                ("GET", "/monthlyRecharge/rollingText", ok([])),
                ("POST", "/monthlyRecharge/signIn", ok()),
                ("GET", "/monthlyRecharge/init", ok(fresh)),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/7/claim",
                    ok({"status": "CLAIMED"}),
                ),
                ("GET", "/monthlyRecharge/init", ok(claimed)),
            ]
        )
        await self.task.execute()
        self.assertFalse(self.responses)

    async def test_non_target_never_signs_or_claims(self):
        data = activity()
        data["isTargetUser"] = False
        self.mock_api([("GET", "/monthlyRecharge/init", ok(data))])
        await self.task.execute()
        self.assertFalse(self.responses)

    async def test_normal_day_draws_once_even_with_large_remaining_count(self):
        initial = activity(remain=99, festival="false")
        self.mock_api(
            self.task_responses(initial)
            + [
                ("POST", "/monthlyRecharge/lottery", ok(0)),
                ("GET", "/monthlyRecharge/init", ok(activity(remain=98))),
            ]
        )
        await self.task.execute()
        self.assertFalse(self.responses)
        self.assertEqual(sum(path.endswith("/lottery") for _, path, _ in self.calls), 1)
        self.assertTrue(any("5元话费券" in m for m in self.messages))

    async def test_festival_has_two_draw_cap_with_read_back(self):
        initial = activity(remain=3, festival=True)
        self.mock_api(
            self.task_responses(initial)
            + [
                ("POST", "/monthlyRecharge/lottery", ok(0)),
                ("GET", "/monthlyRecharge/init", ok(activity(remain=2, festival=True))),
                ("POST", "/monthlyRecharge/lottery", ok(1)),
                ("GET", "/monthlyRecharge/init", ok(activity(remain=1, festival=True))),
            ]
        )
        await self.task.execute()
        self.assertFalse(self.responses)
        self.assertTrue(any("30元话费券" in m for m in self.messages))

    async def test_stale_draw_count_stops(self):
        initial = activity(remain=3, festival=True)
        self.mock_api(
            self.task_responses(initial)
            + [
                ("POST", "/monthlyRecharge/lottery", ok(0)),
                ("GET", "/monthlyRecharge/init", ok(initial)),
            ]
        )
        await self.task.execute()
        self.assertFalse(self.responses)
        self.assertTrue(any("次数未减少" in m for m in self.messages))

    async def test_uncertain_draw_is_not_resubmitted(self):
        initial = activity(remain=3, festival=True)
        self.mock_api(
            self.task_responses(initial)
            + [
                (
                    "POST",
                    "/monthlyRecharge/lottery",
                    {"code": -1, "error": "timeout", "data": None},
                ),
            ]
        )
        await self.task.execute()
        self.assertFalse(self.responses)
        self.assertTrue(any("结果不确定" in m for m in self.messages))

    async def test_click_log_matches_applet_event_type(self):
        self.task.api = AsyncMock(return_value=ok())
        await self.task.click_log("SIGN_CLICK", "签到点击", "signIn", "A")
        self.assertEqual(
            self.task.api.await_args.kwargs["params"]["eventType"], "CLICK"
        )

    async def test_prewarm_claim_waits_for_top_of_hour_then_claims(self):
        sleep_mock = AsyncMock()
        self.mock_api(
            [
                # 08:59:10 初始查询
                (
                    "GET",
                    "/monthlyRecharge/init",
                    ok(activity(disabled=True, nodes=[node(7)])),
                ),
                # 08:59:54 开场前预热连接并刷新资格（避开 09:00 洪峰）
                (
                    "GET",
                    "/monthlyRecharge/init",
                    ok(activity(disabled=True, nodes=[node(7)])),
                ),
                # 09:00:00 直接提交抢券，不再被慢 init 阻塞
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/7/claim",
                    ok({"status": "CLAIMED"}),
                ),
                (
                    "GET",
                    "/monthlyRecharge/init",
                    ok(activity(nodes=[node(7, "CLAIMED")])),
                ),
            ]
        )
        with (
            patch("cu_tasks.tasks.shangdu.CLAIM_WAIT_MAX_SECONDS", 180),
            patch("cu_tasks.tasks.shangdu.asyncio.sleep", new=sleep_mock),
            patch("cu_tasks.tasks.shangdu.datetime") as dt,
        ):
            dt.now.return_value = datetime(2026, 11, 16, 8, 59, 10)
            await self.task.execute(claim_only=True)
        self.assertFalse(self.responses)
        sleeps = [round(c.args[0], 2) for c in sleep_mock.await_args_list]
        self.assertEqual(sleeps, [44.0, 6.02])
        self.assertTrue(any("等待 09:00 放券场次开放 50秒" in m for m in self.messages))
        self.assertTrue(any("已读回 CLAIMED" in m for m in self.messages))

    async def test_prewarm_claim_skips_wait_without_claimable_node(self):
        self.mock_api(
            [
                (
                    "GET",
                    "/monthlyRecharge/init",
                    ok(activity(disabled=True, nodes=[node(7, "CLAIMED")])),
                ),
            ]
        )
        with (
            patch("cu_tasks.tasks.shangdu.CLAIM_WAIT_MAX_SECONDS", 180),
            patch("cu_tasks.tasks.shangdu.datetime") as dt,
        ):
            dt.now.return_value = datetime(2026, 11, 16, 8, 59, 10)
            await self.task.execute(claim_only=True)
        self.assertFalse(self.responses)
        self.assertFalse(any("等待" in m for m in self.messages))
        self.assertTrue(any("无需提交" in m for m in self.messages))

    async def test_monthly_sign_cap_skips_sign_post_and_continues_to_festival_lottery(
        self,
    ):
        initial = activity(sign=0, remain=1, festival=True, count=14)
        after_draw = activity(sign=0, remain=0, festival=True, count=14)
        self.mock_api(
            [
                ("GET", "/monthlyRecharge/init", ok(initial)),
                ("GET", "/monthlyRecharge/rollingText", ok([])),
                ("GET", "/monthlyRecharge/init", ok(initial)),
                ("POST", "/monthlyRecharge/lottery", ok(0)),
                ("GET", "/monthlyRecharge/init", ok(after_draw)),
            ]
        )
        await self.task.execute()
        self.assertFalse(self.responses)
        self.task.click_log.assert_awaited_once_with(
            "LOTTERY_CLICK", "抽奖点击", "lottery", "C"
        )
        self.assertTrue(any("当月签到已满" in m for m in self.messages))
        self.assertTrue(any("30元话费券" in m for m in self.messages))
        self.assertFalse(any("失败" in m for m in self.messages))

    async def test_busy_claim_retries_when_still_claimable_and_succeeds(self):
        still_open = activity(disabled=False, nodes=[node(14)])
        claimed = activity(disabled=False, nodes=[node(14, "CLAIMED")])
        self.mock_api(
            [
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "BUSY"}),
                ),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "CLAIMED"}),
                ),
                ("GET", "/monthlyRecharge/init", ok(claimed)),
            ]
        )
        await self.task.claim_rewards(still_open)
        self.assertFalse(self.responses)
        self.assertTrue(any("重试 1/" in m for m in self.messages))
        self.assertTrue(any("已读回 CLAIMED" in m for m in self.messages))

    async def test_busy_claim_round_robins_other_node_before_retrying_and_init(self):
        open_both = activity(disabled=False, nodes=[node(7), node(14)])
        second_claimed = activity(disabled=True, nodes=[node(7, "CLAIMED"), node(14)])
        self.mock_api(
            [
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "BUSY"}),
                ),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/7/claim",
                    ok({"status": "CLAIMED"}),
                ),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "BUSY"}),
                ),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "BUSY"}),
                ),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "BUSY"}),
                ),
                ("GET", "/monthlyRecharge/init", ok(second_claimed)),
            ]
        )
        await self.task.claim_rewards(open_both)
        self.assertFalse(self.responses)
        self.assertTrue(
            any("14 天节点系统繁忙(BUSY)未领到" in m for m in self.messages)
        )
        self.assertTrue(any("7 天节点奖励领取成功" in m for m in self.messages))

    async def test_prewarm_failure_keeps_initial_state_and_still_claims_at_top_of_hour(
        self,
    ):
        sleep_mock = AsyncMock()
        self.mock_api(
            [
                (
                    "GET",
                    "/monthlyRecharge/init",
                    ok(activity(disabled=True, nodes=[node(14)])),
                ),
                (
                    "GET",
                    "/monthlyRecharge/init",
                    {"code": 502, "data": None},
                ),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "CLAIMED"}),
                ),
                (
                    "GET",
                    "/monthlyRecharge/init",
                    ok(activity(nodes=[node(14, "CLAIMED")])),
                ),
            ]
        )
        with (
            patch("cu_tasks.tasks.shangdu.CLAIM_WAIT_MAX_SECONDS", 180),
            patch("cu_tasks.tasks.shangdu.asyncio.sleep", new=sleep_mock),
            patch("cu_tasks.tasks.shangdu.datetime") as dt,
        ):
            dt.now.return_value = datetime(2026, 11, 18, 10, 59, 10)
            await self.task.execute(claim_only=True)
        self.assertFalse(self.responses)
        self.assertTrue(any("沿用初始资格直发" in m for m in self.messages))
        self.assertTrue(any("14 天节点奖励领取成功" in m for m in self.messages))

    async def test_both_claimable_nodes_submit_14_then_7_before_single_read_back(self):
        open_both = activity(disabled=False, nodes=[node(7), node(14)])
        both_claimed = activity(
            disabled=True, nodes=[node(7, "CLAIMED"), node(14, "CLAIMED")]
        )
        self.mock_api(
            [
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "CLAIMED"}),
                ),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/7/claim",
                    ok({"status": "CLAIMED"}),
                ),
                ("GET", "/monthlyRecharge/init", ok(both_claimed)),
            ]
        )
        await self.task.claim_rewards(open_both)
        self.assertFalse(self.responses)
        self.assertTrue(any("14 天节点奖励领取成功" in m for m in self.messages))
        self.assertTrue(any("7 天节点奖励领取成功" in m for m in self.messages))

    async def test_exhausted_receipt_at_final_session_moves_to_next_node(self):
        open_both = activity(disabled=False, nodes=[node(7), node(14)])
        second_claimed = activity(
            disabled=True, nodes=[node(7, "CLAIMED"), node(14, "EXHAUSTED")]
        )
        self.mock_api(
            [
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "EXHAUSTED"}),
                ),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/7/claim",
                    ok({"status": "CLAIMED"}),
                ),
                ("GET", "/monthlyRecharge/init", ok(second_claimed)),
            ]
        )
        await self.task.claim_rewards(open_both)
        self.assertFalse(self.responses)
        self.assertTrue(any("7 天节点奖励领取成功" in m for m in self.messages))

    async def test_not_started_hint_at_window_edge_retries_and_reads_back_claimed(self):
        pre_open = activity(disabled=True, nodes=[node(14)])
        claimed = activity(disabled=False, nodes=[node(14, "CLAIMED")])
        self.mock_api(
            [
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    {"code": 200, "data": {"code": 500, "msg": "本场活动暂未开始"}},
                ),
                (
                    "POST",
                    "/monthlyRecharge/signIn/reward/14/claim",
                    ok({"status": "CLAIMED"}),
                ),
                ("GET", "/monthlyRecharge/init", ok(claimed)),
            ]
        )
        await self.task.claim_rewards(pre_open, window_opened=True)
        self.assertFalse(self.responses)
        self.assertTrue(any("重试 1/" in m for m in self.messages))
        self.assertTrue(any("已读回 CLAIMED" in m for m in self.messages))
