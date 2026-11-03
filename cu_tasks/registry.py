"""任务注册与选择规则：顺序唯一、名称校验、去重、排除优先。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskSpec:
    name: str
    method: str
    label: str
    needs_cloud: bool = False


TASKS = (
    TaskSpec("market", "market_task", "权益超市"),
    TaskSpec("sign", "sign_task", "签到区"),
    TaskSpec("welfare", "welfare_task", "福利商城九宫格"),
    TaskSpec("xj", "xj_task", "新疆联通"),
    TaskSpec("sec", "sec_task", "安全管家"),
    TaskSpec("farm", "farm_task", "通通乡村"),
    TaskSpec("shangdu", "shangdu_task", "商都福利"),
    TaskSpec("uphone", "uphone_task", "云手机积分"),
    TaskSpec("woread", "woread_task", "沃阅读积分"),
)

# 暂停全部云盘活动；恢复时将对应条目移回 TASKS。
PAUSED_TASKS = (
    TaskSpec("campus", "campus_task", "校园季", needs_cloud=True),
    TaskSpec("battle", "cloud_battle_task", "上传大比拼", needs_cloud=True),
)


def select_tasks(only: str = "", skip: str = "") -> tuple[TaskSpec, ...]:
    """未知名称直接报错，避免拼写错误导致意外执行；保留指定顺序。"""
    registered = {task.name: task for task in TASKS + PAUSED_TASKS}
    selected = list(dict.fromkeys(n.strip() for n in only.split(",") if n.strip()))
    excluded = {n.strip() for n in skip.split(",") if n.strip()}
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
    paused = {task.name for task in PAUSED_TASKS}
    requested_paused = set(selected) & paused
    if requested_paused:
        raise ValueError(f"云盘活动已暂停: {','.join(sorted(requested_paused))}")
    return tuple(
        registered[name]
        for name in (selected or [task.name for task in TASKS])
        if name not in excluded
    )
