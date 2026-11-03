import unittest
from unittest.mock import AsyncMock, patch

import httpx

from cu import Unicom


class CampusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.account = Unicom("test-token")
        self.addAsyncCleanup(self.account.close)
        self.messages = []
        self.account.tlog = self.messages.append

    async def test_course_can_use_second_episode(self):
        self.account.mbh_channel_items = AsyncMock(return_value=[("course", "课程")])
        self.account.wo_content_package_items = AsyncMock(
            return_value=[("ep1", "第1集", 1), ("ep2", "第2集", 2)]
        )
        self.account.wo_ticket = AsyncMock(return_value="ticket")
        self.account.wo_enter = AsyncMock(return_value=("referer", "h5-token"))
        self.account.wo_authorize_edu = AsyncMock()
        self.account.wo_transfer_status = AsyncMock(side_effect=[True, False])
        self.account.wo_file_transfer = AsyncMock(return_value=(True, "成功"))
        ok, name = await self.account.mbh_channel_transfer(
            ("subject",), space_type="76", package_mode=True
        )
        self.assertTrue(ok)
        self.assertEqual(name, "第2集")
        self.account.wo_file_transfer.assert_awaited_once_with(
            "course", "ep2", "第2集", 2, "referer", "76"
        )

    async def test_scan_cap_and_unknown_status_never_transfer(self):
        self.account.mbh_channel_items = AsyncMock(return_value=[("course", "课程")])
        self.account.wo_content_package_items = AsyncMock(
            return_value=[(f"ep{i}", str(i), 1) for i in range(50)]
        )
        self.account.wo_ticket = AsyncMock(return_value="ticket")
        self.account.wo_enter = AsyncMock(return_value=("referer", ""))
        self.account.wo_transfer_status = AsyncMock(return_value=None)
        self.account.wo_file_transfer = AsyncMock()
        ok, message = await self.account.mbh_channel_transfer(
            ("subject",), package_mode=True, max_tries=3
        )
        self.assertFalse(ok)
        self.assertIn("状态查询失败 3", message)
        self.assertEqual(self.account.wo_transfer_status.await_count, 3)
        self.account.wo_file_transfer.assert_not_awaited()

    async def test_upload_500_with_progress_does_not_upload_twice(self):
        self.account.campus_upload = AsyncMock(return_value=(False, "HTTP 500"))
        self.account.campus_task_progress = AsyncMock(return_value={"30004": (3, 5)})
        with patch("cu_tasks.campus.asyncio.sleep", new_callable=AsyncMock):
            ok = await self.account.campus_upload_checked(2)
        self.assertTrue(ok)
        self.account.campus_upload.assert_awaited_once()

    async def test_unconfirmed_upload_does_not_retry(self):
        self.account.campus_upload = AsyncMock(return_value=(False, "HTTP 500"))
        self.account.campus_task_progress = AsyncMock(return_value={"30004": (2, 5)})
        with patch("cu_tasks.campus.asyncio.sleep", new_callable=AsyncMock):
            ok = await self.account.campus_upload_checked(2)
        self.assertFalse(ok)
        self.account.campus_upload.assert_awaited_once()

    async def test_progress_protocol_errors_are_not_unfinished_tasks(self):
        for body in (
            {},
            {"meta": {"code": 500}},
            {
                "meta": {"code": 200},
                "result": {
                    "taskList": [
                        {"taskCode": "30004", "doneCount": True, "dailyLimit": 5}
                    ]
                },
            },
        ):
            self.account.campus_signed_post = AsyncMock(return_value=body)
            with self.assertRaises(ValueError):
                await self.account.campus_task_progress()

    async def test_course_parser_skips_bad_items_and_duplicate_ids(self):
        self.account.req = AsyncMock(
            return_value={
                "data": {
                    "contentPackageDetail": {
                        "mobVodDetails": [
                            None,
                            {},
                            {
                                "id": "ep1",
                                "name": "第一 集",
                                "mediaFiles": [{"fileSize": ["10"]}],
                            },
                            {"id": "ep1"},
                            {"id": "ep2", "name": "第二集"},
                        ]
                    }
                }
            }
        )
        self.assertEqual(
            await self.account.wo_content_package_items("course"),
            [("ep1", "第一集", 10), ("ep2", "第二集", 0)],
        )

    async def test_upload_http_errors_and_non_object_responses(self):
        await self.account.client.aclose()
        self.account.cloud_disk = {"userToken": "test-session"}
        self.account.battle_encrypt_fileinfo = lambda *args: "encrypted"
        for response, expected in (
            (httpx.Response(500, json={"code": "0000"}), "HTTP 500"),
            (httpx.Response(200, json=[]), "响应格式错误"),
            (httpx.Response(503, text="Internal Server Error"), "HTTP 503"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response)
            ) as client:
                self.account.client = client
                ok, message = await self.account.cloud_upload2c(
                    "https://example.test/upload",
                    "1.txt",
                    b"1",
                    referer="https://example.test/",
                )
                self.assertFalse(ok)
                self.assertIn(expected, message)
