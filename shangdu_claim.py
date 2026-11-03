"""商都签到节点券专用入口；青龙 cron：0 9,11,15,19 * * *。"""

import asyncio

from cu_tasks.runner import main
from cu_tasks.tasks.shangdu import ShangduClaimTask

if __name__ == "__main__":
    asyncio.run(main([ShangduClaimTask], notify=False))
