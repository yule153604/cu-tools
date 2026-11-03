"""联通脚本入口（青龙：task cu.py）；实现见 cu_tasks。"""

import asyncio

from cu_tasks.runner import main

if __name__ == "__main__":
    asyncio.run(main())
