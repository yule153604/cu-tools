"""商都签到节点券专用入口；青龙 cron：0 9,11,15,19 * * *。"""

import asyncio
import os
import time

from cu import Unicom


async def run_account(token, index):
    account = Unicom(token, index)
    try:
        if await account.login():
            await account.shangdu_task(claim_only=True)
    except Exception as exc:
        account.tlog_exc(exc, "商都节点券领取")
    finally:
        await account.close()


async def main():
    os.environ["TZ"] = "Asia/Shanghai"
    if hasattr(time, "tzset"):
        time.tzset()
    tokens = [
        value.strip()
        for value in os.environ.get("chinaUnicomCookie", "").split("@")
        if value.strip()
    ]
    if not tokens:
        print("未找到环境变量 chinaUnicomCookie", flush=True)
        return
    await asyncio.gather(
        *(run_account(token, index) for index, token in enumerate(tokens, start=1))
    )


if __name__ == "__main__":
    asyncio.run(main())
