"""调度：多账号并发；账号内按 Settings.task_concurrency 并发执行活动并隔离异常。"""

import asyncio
import os
import threading
import time
from collections.abc import Sequence
from datetime import datetime

from cu_tasks.base import Task
from cu_tasks.common import format_exception_detail
from cu_tasks.config import Settings
from cu_tasks.session import Account

NOTIFY_TITLE = "中国联通任务"
NOTIFY_KEYWORDS = ("失败", "异常", "成功", "抽奖")
NOTIFY_MAX_LINES = 100


async def run_task(task: Task, gate: asyncio.Semaphore):
    async with gate:
        try:
            if not task.applicable():
                task.tlog(f"归属地 {task.province or '未知'} 不在活动范围，跳过")
                return
            task.tlog("开始")
            await task.run()
        except Exception as exc:
            task.tlog_exc(exc, task.name)
        finally:
            await task.close()


async def run_account(
    account: Account, task_types: Sequence[type[Task]], settings: Settings
) -> list[str]:
    """登录后执行任务；返回按注册顺序整理的日志行。始终释放连接。"""
    tasks: list[Task] = []
    try:
        if not await account.login():
            return list(account.lines)
        tasks = [cls(account, settings) for cls in task_types]
        gate = asyncio.Semaphore(settings.task_concurrency)
        running = [asyncio.ensure_future(run_task(t, gate)) for t in tasks]
        try:
            await asyncio.gather(*running)
        except BaseException:
            # 取消等外部信号：先收拢子任务再向上传递，避免关闭连接后仍有请求
            for fut in running:
                fut.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            raise
    finally:
        await account.close()
    return account.lines + [line for t in tasks for line in t.lines]


def read_tokens(env=None) -> list[str]:
    raw = (os.environ if env is None else env).get("chinaUnicomCookie", "")
    return [t.strip() for t in raw.split("@") if t.strip()]


def setup_timezone():
    # 10 点开抢、日期比对都依赖本地时区，容器 TZ 为空（含空串）时显式钉死
    if not os.environ.get("TZ"):
        os.environ["TZ"] = "Asia/Shanghai"
    if hasattr(time, "tzset"):
        time.tzset()


def notify_lines(lines: list[str]) -> list[str]:
    picked = [line for line in lines if any(k in line for k in NOTIFY_KEYWORDS)]
    if len(picked) > NOTIFY_MAX_LINES:
        rest = len(picked) - NOTIFY_MAX_LINES
        picked = picked[:NOTIFY_MAX_LINES] + [f"... 其余 {rest} 行已省略"]
    return picked


def notify_send(title, content):
    # notify.py 的一言接口无超时、推送线程 join 也无超时，必须隔离，否则会卡死青龙任务槽
    os.environ.setdefault("HITOKOTO", "false")

    def _send():
        try:
            from notify import send

            send(title, content)
        except Exception as e:
            print(f"推送失败: {e}")

    worker = threading.Thread(target=_send, daemon=True)
    worker.start()
    worker.join(timeout=30)
    if worker.is_alive():
        print("推送超时 30s，放弃等待")


async def main(task_types: Sequence[type[Task]] | None = None, *, notify=True):
    """入口：校验配置 → 各账号并发执行 → 汇总推送。task_types 为空时按环境变量选择。"""
    setup_timezone()
    print(f"开始: {datetime.now():%Y-%m-%d %H:%M:%S} TZ={os.environ.get('TZ')}")
    try:
        settings = Settings.from_env()
        if task_types is None:
            from cu_tasks.registry import select_tasks

            task_types = select_tasks(settings.only_tasks, settings.skip_tasks)
    except ValueError as exc:
        print(f"配置错误: {exc}", flush=True)
        return
    if not task_types:
        print("没有需要执行的任务")
        return
    tokens = read_tokens()
    if not tokens:
        print("未找到环境变量 chinaUnicomCookie")
        return

    print(f"启动 {len(tokens)} 个账号，任务: {','.join(t.name for t in task_types)}")
    accounts = [Account(token, idx) for idx, token in enumerate(tokens, start=1)]
    results = await asyncio.gather(
        *(run_account(a, task_types, settings) for a in accounts),
        return_exceptions=True,
    )
    lines: list[str] = []
    for account, result in zip(accounts, results):
        if isinstance(result, BaseException):
            line = f"账号{account.idx} 未捕获异常: {format_exception_detail(result)}"
            print(line, flush=True)
            lines.append(line)
        else:
            lines.extend(result)
    if notify and (picked := notify_lines(lines)):
        notify_send(NOTIFY_TITLE, "\n".join(picked))
    print("结束")
