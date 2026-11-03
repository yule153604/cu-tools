"""任务注册与选择规则：顺序唯一、名称校验、去重、排除优先。"""

from cu_tasks.base import Task
from cu_tasks.tasks.farm import FarmTask
from cu_tasks.tasks.market import MarketTask
from cu_tasks.tasks.security import SecurityTask
from cu_tasks.tasks.shangdu import ShangduTask
from cu_tasks.tasks.sign import SignTask
from cu_tasks.tasks.uphone import UphoneTask
from cu_tasks.tasks.welfare import WelfareTask
from cu_tasks.tasks.woread import WoreadTask
from cu_tasks.tasks.xinjiang import XinjiangTask

# 顺序即串行（UNICOM_TASK_CONCURRENCY=1）时的执行顺序与通知汇总顺序。
# 权益超市排第一：10 点开抢需要尽早进入等待。
TASKS: tuple[type[Task], ...] = (
    MarketTask,
    SignTask,
    WelfareTask,
    XinjiangTask,
    SecurityTask,
    FarmTask,
    ShangduTask,
    UphoneTask,
    WoreadTask,
)


def _names(raw: str) -> list[str]:
    return list(dict.fromkeys(n.strip() for n in raw.split(",") if n.strip()))


def select_tasks(only: str = "", skip: str = "") -> tuple[type[Task], ...]:
    """未知名称直接报错，避免拼写错误导致意外执行；保留指定顺序。"""
    registered = {task.name: task for task in TASKS}
    selected, excluded = _names(only), set(_names(skip))
    for setting, names in (
        ("UNICOM_ONLY_TASK", selected),
        ("UNICOM_SKIP_TASK", excluded),
    ):
        unknown = set(names) - registered.keys()
        if unknown:
            raise ValueError(
                f"{setting} 含未知任务名: {','.join(sorted(unknown))}；"
                f"可选: {','.join(registered)}"
            )
    if only.strip() and not selected:
        raise ValueError("UNICOM_ONLY_TASK 未包含有效任务名")
    return tuple(
        registered[name] for name in (selected or registered) if name not in excluded
    )
