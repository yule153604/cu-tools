"""商都签到节点券专用入口；青龙 cron：59 8,10,14,18 * * *。"""

import asyncio

from cu_tasks.runner import main
from cu_tasks.tasks.shangdu import ShangduClaimTask

if __name__ == "__main__":
    asyncio.run(main([ShangduClaimTask], notify=False))
