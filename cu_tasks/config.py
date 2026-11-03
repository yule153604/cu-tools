"""运行配置：启动时一次性读取并校验全部环境变量，任务只读 Settings。

配置错误在登录前统一报出，不会在导入模块时崩溃，也不会跑到一半才发现。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

WELFARE_DRAW_HARD_LIMIT = 10
TASK_CONCURRENCY_HARD_LIMIT = 9


def read_int(env, name, default, minimum=None, maximum=None):
    """未设置才用 default；空串/非整数/越界一律报错，避免静默回退。"""
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"环境变量 {name}={raw!r} 不是合法整数") from e
    if minimum is not None and value < minimum:
        raise ValueError(f"环境变量 {name}={raw!r} 低于最小值 {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"环境变量 {name}={raw!r} 超过最大值 {maximum}")
    return value


def read_str(env, name, default):
    value = env.get(name, default).strip()
    if not value:
        raise ValueError(f"环境变量 {name} 不能为空")
    return value


@dataclass(frozen=True)
class Settings:
    only_tasks: str = ""
    skip_tasks: str = ""
    # 同一账号内并发执行的活动数；1 为严格按注册顺序串行
    task_concurrency: int = 3

    welfare_activity_id: str = "AC20260920094443"
    welfare_source: str = "ZXGS97000020362"
    welfare_max_draws: int = 3

    farm_garbage_wait: int = 28
    farm_max_charge_per_land: int = 20
    farm_harvest_wait: int = 3

    market_wait_max: int = 300
    market_api_retries: int = 3

    # None 表示按活动规则（普通日 1 / 节气日 2），用户值只能进一步收紧
    shangdu_lottery_max: int | None = None
    uphone_lottery_cost: int = 100

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        d = cls()
        return cls(
            only_tasks=env.get("UNICOM_ONLY_TASK", ""),
            skip_tasks=env.get("UNICOM_SKIP_TASK", ""),
            task_concurrency=read_int(
                env,
                "UNICOM_TASK_CONCURRENCY",
                d.task_concurrency,
                minimum=1,
                maximum=TASK_CONCURRENCY_HARD_LIMIT,
            ),
            welfare_activity_id=read_str(
                env, "UNICOM_WELFARE_ACTIVITY_ID", d.welfare_activity_id
            ),
            welfare_source=read_str(env, "UNICOM_WELFARE_FROM", d.welfare_source),
            welfare_max_draws=read_int(
                env,
                "UNICOM_WELFARE_MAX_DRAWS",
                d.welfare_max_draws,
                minimum=0,
                maximum=WELFARE_DRAW_HARD_LIMIT,
            ),
            farm_garbage_wait=read_int(
                env, "UNICOM_TTXC_GARBAGE_WAIT", d.farm_garbage_wait, minimum=0
            ),
            farm_max_charge_per_land=read_int(
                env,
                "UNICOM_TTXC_GROW_MAX_CHARGE_PER_LAND",
                d.farm_max_charge_per_land,
                minimum=0,
            ),
            farm_harvest_wait=read_int(
                env, "UNICOM_TTXC_HARVEST_WAIT", d.farm_harvest_wait, minimum=0
            ),
            market_wait_max=read_int(
                env, "UNICOM_MARKET_WAIT_MAX", d.market_wait_max, minimum=0
            ),
            market_api_retries=read_int(
                env, "UNICOM_MARKET_API_RETRIES", d.market_api_retries, minimum=1
            ),
            shangdu_lottery_max=read_int(
                env, "SHANGDU_LOTTERY_MAX", d.shangdu_lottery_max, minimum=0
            ),
            uphone_lottery_cost=read_int(
                env, "UNICOM_UPHONE_LOTTERY_COST", d.uphone_lottery_cost, minimum=1
            ),
        )
