import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx

from cu_tasks import runner
from cu_tasks.base import Task
from cu_tasks.common import find_set_cookie, redact_text
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
        from cu_tasks.common import redact_order_id

        result = redact_text(
            "ecs_token=private-token ticket=private-ticket 手机号=13800138000 "
            "手机号13912345678已绑定 user_13711112222 "
            "tel=+8613512345678 tel2=8613612345678 tel3=+86-13412345678 "
            "order=1660000888817949740000000001 "
            "tail13=1380013139917949740000000001 "
            "snowflake=1990000000000000001 ts=1794974000000 "
            'Authorization: Bearer eyJ.jwt.sig {"secretKey": "top-secret"} '
            'auth-sa-token=sa-cred {"Cookie": "JSESSIONID=s1; other=s2; Path=/"} '
            'password=my-pass {"token": ""} {"token": "truncated-jwt-value'
        )
        for private in (
            "private-token",
            "private-ticket",
            "13800138000",
            "13912345678",
            "13711112222",
            "13512345678",
            "13612345678",
            "13412345678",
            "16600008888",
            "13800131399",
            "eyJ.jwt.sig",
            "top-secret",
            "sa-cred",
            "JSESSIONID=s1",
            "other=s2",
            "my-pass",
            "truncated-jwt-value",
        ):
            self.assertNotIn(private, result)
        self.assertIn('{"token": ""}', result)
        self.assertIn("snowflake=1990000000000000001", result)
        self.assertIn("ts=1794974000000", result)
        self.assertIn("17949740000000001", result)
        # 二次脱敏保持幂等（不因尾号含 13xx 误吃订单尾号）
        self.assertEqual(redact_text(result), result)
        with_known_mobile = redact_text(
            "custom=abc16600000199xyz hdr=Bearer tok_16600000199_secret_tail "
            '{"account":"MTY2MDAwMDAxOTk="}',
            mobile="16600000199",
        )
        self.assertNotIn("16600000199", with_known_mobile)
        self.assertNotIn("secret_tail", with_known_mobile)
        self.assertNotIn("MTY2MDAwMDAxOTk", with_known_mobile)
        self.assertIn("custom=abc166****0199xyz", with_known_mobile)
        self.assertIn("hdr=Bearer ***", with_known_mobile)
        self.assertEqual(redact_text(with_known_mobile), with_known_mobile)
        self.assertEqual(
            redact_order_id("ORD1380013800017949740000000001"),
            "ORD138****800017949740000000001",
        )

    def test_find_set_cookie_matches_exact_cookie_name(self):
        headers = httpx.Headers(
            [
                ("set-cookie", "other_jea_id=wrong; Path=/"),
                ("set-cookie", "_jea_id=exact-val; Path=/; HttpOnly"),
            ]
        )
        self.assertEqual(find_set_cookie(headers, "jea_id"), "")
        self.assertEqual(find_set_cookie(headers, "_jea_id"), "exact-val")


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

    async def test_default_transport_keeps_warm_connections_past_prefetch_lead(self):
        account = Account("t")
        self.addAsyncCleanup(account.close)
        expiry = getattr(account._transport._pool, "_keepalive_expiry", 0)
        self.assertGreaterEqual(expiry, 30.0)


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

    def test_notify_lines_excludes_procedural_noise_but_keeps_outcomes_and_failures(
        self,
    ):
        sample = [
            "[00:47:02] [账号1] 登录成功: 131****0309 (新疆)",
            "[00:47:02] [账号1] [权益超市] 登录成功",
            "[00:47:02] [账号1] [权益超市] 抽奖次数: 5",
            "[00:47:04] [账号1] [权益超市] 抽奖: 未中奖",
            "[00:47:02] [账号1] [福利商城九宫格] 剩余 0 次，本轮最多抽奖 0 次；活动返回消耗积分配置 ",
            "[00:47:06] [账号3] [福利商城九宫格] 抽奖中奖: 88积分",
            "[00:47:07] [账号3] [福利商城九宫格] 抽奖次数已用完",
            "[00:47:08] [账号3] [福利商城好运开转] 抽奖返回 1129（活动繁忙），查询结果和剩余次数确认",
            "[00:47:15] [账号1] [通通乡村] 签到成功 +125g",
            "[00:47:47] [账号1] [通通乡村] 地块1充能成功",
            "[00:48:00] [账号1] [签到区] 月签有礼领取成功: [累计3天] [话费红包] +0.10元",
            "[00:48:20] [账号1] [云手机积分] 云手机 SSO 成功",
            "[00:48:20] [账号1] [云手机积分] 十连抽奖完成 total=10 won=0 奖品=无",
            "[00:48:20] [账号1] [云手机积分] 十连抽奖完成 total=10 won=1 奖品=腾讯视频月卡",
            "[00:48:20] [账号1] [沃阅读积分] 换票成功",
            "[00:48:20] [账号1] [沃阅读积分] readChapter 读章节成功 (章节数=3)",
            "[00:48:21] [账号1] [沃阅读积分] newreadadd 更新阅读进度成功",
            "[00:48:20] [账号2] [新疆联通] 每日打卡1: 您已打卡成功，每个用户只能打卡一次哦~",
            "[00:48:21] [账号2] [新疆联通] 每月抽奖: 今日抽奖机会已用完，请明天再来吧！",
            "[00:48:22] [账号3] [商都福利] 初始化完成 签到状态=0 抽奖次数=0",
            "[00:48:22] [账号3] [商都福利] 无可用抽奖次数或抽奖上限为 0",
            "[00:48:23] [账号4] 登录失败: 凭证过期",
            "[00:48:24] [账号4] [沃阅读积分] 换票失败: HTTP=502",
        ]
        picked = runner.notify_lines(sample)
        self.assertEqual(
            picked,
            [
                "[00:47:06] [账号3] [福利商城九宫格] 抽奖中奖: 88积分",
                "[00:47:15] [账号1] [通通乡村] 签到成功 +125g",
                "[00:48:00] [账号1] [签到区] 月签有礼领取成功: [累计3天] [话费红包] +0.10元",
                "[00:48:20] [账号1] [云手机积分] 十连抽奖完成 total=10 won=1 奖品=腾讯视频月卡",
                "[00:48:23] [账号4] 登录失败: 凭证过期",
                "[00:48:24] [账号4] [沃阅读积分] 换票失败: HTTP=502",
            ],
        )

    def test_notify_send_skips_thread_when_unconfigured(self):
        import types

        self.assertFalse(runner.notify_configured({}))
        self.assertFalse(runner.notify_configured({"CONSOLE": "false"}))
        self.assertFalse(runner.notify_configured({"TG_BOT_TOKEN": "only-half"}))
        self.assertTrue(runner.notify_configured({"PUSH_KEY": "SCT123"}))
        self.assertTrue(
            runner.notify_configured({"TG_BOT_TOKEN": "tk", "TG_USER_ID": "1"})
        )
        fake_empty = types.SimpleNamespace(
            push_config={"HITOKOTO": True, "BARK_PUSH": ""},
            send=lambda *a, **k: None,
        )
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.dict("sys.modules", {"notify": fake_empty}),
            patch("cu_tasks.runner.threading.Thread") as thread_cls,
        ):
            runner.notify_send("title", "body")
        thread_cls.assert_not_called()

        sent_calls = []
        fake_configured = types.SimpleNamespace(
            push_config={"HITOKOTO": True, "BARK_PUSH": "bark-device"},
            send=lambda title, body: sent_calls.append(
                (title, body, fake_configured.push_config["HITOKOTO"])
            ),
        )
        with (
            patch.dict("os.environ", {"HITOKOTO": "TRUE"}, clear=True),
            patch.dict("sys.modules", {"notify": fake_configured}),
        ):
            runner.notify_send("T", "B")
        self.assertEqual(sent_calls, [("T", "B", False)])

    def test_uphone_winter_config_and_notify_filtering(self):
        cfg = Settings.from_env(
            {
                "UNICOM_UPHONE_ACTIVITY_ID": "HD2027010100001",
                "UNICOM_UPHONE_CHANNEL_ID": "ST-Custom",
            }
        )
        self.assertEqual(
            (cfg.uphone_activity_id, cfg.uphone_channel_id),
            ("HD2027010100001", "ST-Custom"),
        )
        for key in ("UNICOM_UPHONE_ACTIVITY_ID", "UNICOM_UPHONE_CHANNEL_ID"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Settings.from_env({key: "   "})

        self.assertFalse(any("夏日" in s for s in runner.NOTIFY_NOISE))
        sample = [
            "[12:00:09] [账号2] [云手机积分] 冬日领次数成功 2508-01 去赚积分",
            "[12:00:09] [账号2] [云手机积分] 冬日领次数跳过(日限) 2508-01: 今日领取抽奖次数已达上限",
            "[12:00:09] [账号2] [云手机积分] 冬日领次数跳过(日限) 2508-01: 今日已领取成功",
            "[12:00:09] [账号2] [云手机积分] 领奖受限 0514-001: 今日领取抽奖奖励成功达上限",
            "[12:00:09] [账号2] [云手机积分] 冬日抽奖次数 rafflesLeft=0→1",
            "[12:00:12] [账号2] [云手机积分] 冬日抽奖#1: 未中奖 (谢谢参与)",
            "[12:00:12] [账号2] [云手机积分] 冬日抽奖#1 中奖: 滴滴出行5元券 type=1 order=ord-1",
            "[12:00:13] [账号2] [云手机积分] 冬日抽奖结束 draws=1",
            "[12:00:14] [账号3] [云手机积分] 冬日抽奖失败: HTTP=500",
        ]
        self.assertEqual(
            runner.notify_lines(sample),
            [
                "[12:00:12] [账号2] [云手机积分] 冬日抽奖#1 中奖: 滴滴出行5元券 type=1 order=ord-1",
                "[12:00:14] [账号3] [云手机积分] 冬日抽奖失败: HTTP=500",
            ],
        )

    async def test_main_redacts_account_mobile_in_unhandled_exception_and_emit(self):
        acc_sink = []
        account = Account("tok")
        self.addAsyncCleanup(account.close)
        with patch("builtins.print"):
            account.emit("raw token=sec123 tel=13800138000", acc_sink)
        self.assertNotIn("sec123", acc_sink[0])
        self.assertNotIn("13800138000", acc_sink[0])

        async def boom(acc, _types, _settings):
            acc.mobile = "16600000199"
            raise RuntimeError("boom key=abc16600000199xyz b64=MTY2MDAwMDAxOTk=")

        sent = []
        env = {"chinaUnicomCookie": "tok1", "TZ": "Asia/Shanghai"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch.object(runner, "run_account", side_effect=boom),
            patch.object(
                runner, "notify_send", side_effect=lambda t, c: sent.append(c)
            ),
            patch("builtins.print") as mock_print,
        ):
            await runner.main([make_task("x", AsyncMock())], notify=True)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertNotIn("16600000199", printed)
        self.assertNotIn("MTY2MDAwMDAxOTk", printed)
        self.assertIn("abc166****0199xyz", printed)
        self.assertEqual(len(sent), 1)
        self.assertNotIn("16600000199", sent[0])


if __name__ == "__main__":
    unittest.main()
