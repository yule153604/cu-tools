import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx

from cu_tasks import runner
from cu_tasks.base import Task
from cu_tasks.common import redact_text
from cu_tasks.config import Settings
from cu_tasks.jftask import JfTaskClient, task_pending
from cu_tasks.registry import TASKS, select_tasks
from cu_tasks.session import Account
from cu_tasks.tasks.xinjiang import activity_suffix


class RegistryTests(unittest.TestCase):
    def test_defaults_have_implementations_and_no_duplicates(self):
        self.assertEqual(select_tasks(), TASKS)
        names = [t.name for t in TASKS]
        self.assertEqual(len(names), len(set(names)))
        for task in TASKS:
            self.assertTrue(issubclass(task, Task))
            self.assertTrue(task.name and task.label)
            self.assertIsNot(task.run, Task.run)

    def test_cloud_tasks_are_gone(self):
        for name in ("campus", "battle"):
            with self.assertRaises(ValueError):
                select_tasks(name)

    def test_selection_order_deduplication_and_skip(self):
        self.assertEqual(
            [t.name for t in select_tasks("welfare,sign,welfare,woread", "sign")],
            ["welfare", "woread"],
        )

    def test_unknown_names_are_errors(self):
        for only, skip in (("sign,typo", ""), ("", "typo"), ("ltzf", ""), (",,", "")):
            with self.assertRaises(ValueError):
                select_tasks(only, skip)

    def test_sensitive_log_values_are_redacted(self):
        result = redact_text(
            "ecs_token=private-token ticket=private-ticket 手机号=13800138000"
        )
        for private in ("private-token", "private-ticket", "13800138000"):
            self.assertNotIn(private, result)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(Settings.from_env({}), Settings())

    def test_values_are_parsed(self):
        s = Settings.from_env(
            {
                "UNICOM_TASK_CONCURRENCY": "1",
                "SHANGDU_LOTTERY_MAX": "0",
                "UNICOM_TTXC_GARBAGE_WAIT": "5",
            }
        )
        self.assertEqual(
            (s.task_concurrency, s.shangdu_lottery_max, s.farm_garbage_wait),
            (1, 0, 5),
        )

    def test_invalid_values_raise(self):
        for name, value in (
            ("UNICOM_TASK_CONCURRENCY", "0"),
            ("UNICOM_TASK_CONCURRENCY", "x"),
            ("UNICOM_MARKET_API_RETRIES", "0"),
            ("UNICOM_TTXC_HARVEST_WAIT", "-1"),
            ("UNICOM_UPHONE_LOTTERY_COST", ""),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                Settings.from_env({name: value})


class HelperTests(unittest.TestCase):
    def test_xinjiang_activity_suffix(self):
        self.assertEqual(activity_suffix(datetime(2026, 11, 4)), "Nov2026Act")

    def test_task_pending(self):
        self.assertTrue(task_pending({"finishCount": 0, "needCount": 1}))
        self.assertTrue(task_pending({}))
        self.assertFalse(task_pending({"finishCount": 2, "needCount": 2}))
        self.assertFalse(task_pending({"finishCount": "x"}))


def make_task(name, body, *, provinces=()):
    """构造一次性任务类；body(task) 为 run 的实现。"""

    async def run(self):
        await body(self)

    return type(
        f"T_{name}",
        (Task,),
        {"name": name, "label": name, "provinces": provinces, "run": run},
    )


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.account = Account("test-token", transport=httpx.MockTransport(self.handle))
        self.account.login = AsyncMock(return_value=True)
        self.account.province = "广东"

    def handle(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async def run_tasks(self, *types, concurrency=3):
        return await runner.run_account(
            self.account, types, Settings(task_concurrency=concurrency)
        )

    async def test_failure_is_isolated_and_logged_under_its_task(self):
        seen = []

        async def fail(task):
            raise ValueError("simulated")

        async def ok(task):
            seen.append(task.label)
            task.tlog("完成")

        lines = await self.run_tasks(make_task("a", fail), make_task("b", ok))
        self.assertEqual(seen, ["b"])
        self.assertTrue(any("[a]" in x and "simulated" in x for x in lines))
        self.assertTrue(self.account.closed)

    async def test_lines_follow_registry_order_even_when_concurrent(self):
        async def slow(task):
            await asyncio.sleep(0.02)
            task.tlog("slow-done")

        async def fast(task):
            task.tlog("fast-done")

        lines = await self.run_tasks(make_task("slow", slow), make_task("fast", fast))
        body = [x for x in lines if "done" in x]
        self.assertIn("slow-done", body[0])
        self.assertIn("fast-done", body[1])

    async def test_concurrency_limit_is_respected(self):
        active = peak = 0

        async def body(task):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

        types = [make_task(f"t{i}", body) for i in range(5)]
        await self.run_tasks(*types, concurrency=2)
        self.assertEqual(peak, 2)
        await Account.close(self.account)
        active = peak = 0
        self.account = Account("t", transport=httpx.MockTransport(self.handle))
        self.account.login = AsyncMock(return_value=True)
        await self.run_tasks(*types, concurrency=1)
        self.assertEqual(peak, 1)

    async def test_task_cookies_are_isolated_but_seeded_from_login(self):
        self.account.client.cookies.set("ecs_token", "login", domain=".10010.com")

        async def writer(task):
            task.client.cookies.set("ecs_token", "mutated", domain=".10010.com")
            await task.req("GET", "https://m.client.10010.com/a")

        async def reader(task):
            await asyncio.sleep(0.01)
            await task.req("GET", "https://m.client.10010.com/b")

        await self.run_tasks(make_task("w", writer), make_task("r", reader))
        cookies = {r.url.path: r.headers.get("cookie", "") for r in self.requests}
        self.assertIn("ecs_token=mutated", cookies["/a"])
        self.assertIn("ecs_token=login", cookies["/b"])
        self.assertEqual(self.account.client.cookies.get("ecs_token"), "login")

    async def test_province_gate_skips_without_running(self):
        body = AsyncMock()
        lines = await self.run_tasks(make_task("xj", body, provinces=("新疆",)))
        body.assert_not_awaited()
        self.assertTrue(any("跳过" in x for x in lines))

    async def test_login_failure_runs_nothing_and_closes(self):
        self.account.login.return_value = False
        body = AsyncMock()
        await self.run_tasks(make_task("a", body))
        body.assert_not_awaited()
        self.assertTrue(self.account.closed)

    async def test_cancellation_propagates_and_closes(self):
        async def cancel(task):
            raise asyncio.CancelledError

        sibling_done = []

        async def sibling(task):
            await asyncio.sleep(1)
            sibling_done.append(True)

        with self.assertRaises(asyncio.CancelledError):
            await self.run_tasks(make_task("c", cancel), make_task("s", sibling))
        self.assertEqual(sibling_done, [])
        self.assertTrue(self.account.closed)

    async def test_login_expiry_reason_is_logged(self):
        account = Account("t")
        self.addAsyncCleanup(account.close)
        account.req = AsyncMock(
            return_value={
                "code": 200,
                "data": {"code": "1", "dsc": "您的登录凭证已失效，请重新登录"},
            }
        )
        self.assertFalse(await account.login())
        self.assertTrue(any("登录凭证已失效" in m for m in account.lines))

    async def test_get_ticket_keeps_landing_url_per_task(self):
        def handle(request):
            to_url = request.url.params["to_url"]
            return httpx.Response(
                302, headers={"Location": f"{to_url}?ticket=T-{to_url[-1]}"}
            )

        account = Account("t", transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(account.close)
        t1, t2 = Task(account), Task(account)
        r1, r2 = await asyncio.gather(
            t1.get_ticket("https://x/1"), t2.get_ticket("https://x/2")
        )
        self.assertEqual((r1, r2), ("T-1", "T-2"))
        self.assertTrue(t1.last_ticket_url.endswith("T-1"))
        self.assertTrue(t2.last_ticket_url.endswith("T-2"))


class JfTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_secret_key_and_signed_headers(self):
        seen = []

        def handle(request):
            seen.append(request)
            if request.url.path.endswith("getSecretKey"):
                return httpx.Response(
                    200,
                    headers={"Set-Cookie": "_jea_id=J1; Path=/"},
                    json={"code": "0000", "data": {"secretKey": "k"}},
                )
            return httpx.Response(200, json={"code": "0000"})

        account = Account("t", transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(account.close)
        task = Task(account)
        jf = JfTaskClient(
            task,
            ticket="tk",
            partners_id="1702",
            client_type="c",
            user_agent="ua",
            signed_extra={"Origin": "o"},
        )
        self.assertTrue(await jf.init(extra_headers={"pageId": "p"}))
        await jf.post("jftask/receive", {"taskCode": "x"})
        key_req, post_req = seen
        self.assertEqual(key_req.headers["pageId"], "p")
        self.assertNotIn("origin", key_req.headers)
        self.assertEqual(post_req.headers["cookie"], "_jea_id=J1")
        self.assertEqual(post_req.headers["origin"], "o")
        self.assertEqual(len(post_req.headers["x-request-signature"]), 64)


class MainTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_error_stops_before_login(self):
        env = {"chinaUnicomCookie": "a", "UNICOM_ONLY_TASK": "typo", "TZ": "UTC"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch.object(runner, "run_account") as run_account,
            patch("builtins.print"),
        ):
            await runner.main()
        run_account.assert_not_called()

    async def test_notify_lines_filter_and_cap(self):
        lines = [f"成功 {i}" for i in range(120)] + ["普通日志"]
        picked = runner.notify_lines(lines)
        self.assertEqual(len(picked), 101)
        self.assertNotIn("普通日志", picked)


if __name__ == "__main__":
    unittest.main()
