import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from cu import Unicom
from cu_tasks.common import redact_text
from cu_tasks.registry import TASKS, select_tasks


class RegistryTests(unittest.TestCase):
    def test_defaults_have_implementations_and_no_duplicates(self):
        self.assertEqual(select_tasks(), TASKS)
        self.assertEqual(len(TASKS), len({t.name for t in TASKS}))
        for task in TASKS:
            self.assertTrue(callable(getattr(Unicom, task.method)))
        self.assertFalse(hasattr(Unicom, "ltzf_task"))
        self.assertFalse(hasattr(Unicom, "wocare_post"))

    def test_selection_order_deduplication_and_skip(self):
        self.assertEqual(
            [t.name for t in select_tasks("welfare,sign,welfare,woread", "sign")],
            ["welfare", "woread"],
        )

    def test_paused_cloud_tasks_cannot_be_selected(self):
        self.assertFalse(any(task.needs_cloud for task in select_tasks()))
        for name in ("campus", "battle", "sign,campus"):
            with self.assertRaisesRegex(ValueError, "云盘活动已暂停"):
                select_tasks(name)
        self.assertEqual(select_tasks(skip="campus,battle"), TASKS)

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


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.account = Unicom("test-token")
        self.addAsyncCleanup(self.account.close)
        self.account.login = AsyncMock(return_value=True)
        self.account.cloudpan_login = AsyncMock()
        self.environment = patch.dict(os.environ, {"UNICOM_SKIP_TASK": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_failure_is_isolated_and_tags_are_reset(self):
        os.environ["UNICOM_ONLY_TASK"] = "sign,welfare"
        tags = []

        async def fail():
            raise ValueError("simulated")

        async def next_task():
            tags.append(self.account._task_tag)

        self.account.sign_task = fail
        self.account.welfare_task = next_task
        await self.account.run()
        self.assertEqual(tags, ["福利商城九宫格"])
        self.assertIsNone(self.account._task_tag)
        self.assertTrue(self.account.client.is_closed)
        self.account.cloudpan_login.assert_not_awaited()

    async def test_paused_cloud_tasks_do_not_login_or_run(self):
        os.environ["UNICOM_ONLY_TASK"] = "campus,battle"
        self.account.campus_task = AsyncMock()
        self.account.cloud_battle_task = AsyncMock()
        await self.account.run()
        self.account.login.assert_not_awaited()
        self.account.cloudpan_login.assert_not_awaited()
        self.account.campus_task.assert_not_awaited()
        self.account.cloud_battle_task.assert_not_awaited()

    async def test_invalid_config_does_not_login(self):
        os.environ["UNICOM_ONLY_TASK"] = "sign,typo"
        await self.account.run()
        self.account.login.assert_not_awaited()
        self.assertTrue(self.account.client.is_closed)

    async def test_login_failure_closes_client(self):
        os.environ["UNICOM_ONLY_TASK"] = "sign"
        self.account.login.return_value = False
        await self.account.run()
        self.assertTrue(self.account.client.is_closed)

    async def test_login_expiry_reason_is_logged(self):
        self.account.req = AsyncMock(
            return_value={
                "code": 200,
                "data": {"code": "1", "dsc": "您的登录凭证已失效，请重新登录"},
            }
        )
        messages = []
        self.account.log = messages.append
        self.assertFalse(await Unicom.login(self.account))
        self.assertTrue(any("登录凭证已失效" in m for m in messages))

    async def test_cancellation_propagates_and_closes_client(self):
        os.environ["UNICOM_ONLY_TASK"] = "sign"
        self.account.sign_task = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.account.run()
        self.assertTrue(self.account.client.is_closed)
        self.assertIsNone(self.account._task_tag)
