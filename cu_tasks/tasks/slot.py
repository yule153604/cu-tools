"""福利商城好运开转（老虎机抽奖），协议依据 2026-11-16 HAR；复用 ninePalaceGrid 流程。"""

from cu_tasks.tasks.welfare import NinePalaceGridTask


class SlotTask(NinePalaceGridTask):
    """福利商城积分老虎机抽奖（单次消耗受 UNICOM_SLOT_MAX_COST 约束）。"""

    name, label = "slot", "福利商城好运开转"

    def grid_config(self) -> tuple[str, str, int, int | None]:
        return (
            self.settings.slot_activity_id,
            self.settings.slot_source,
            self.settings.slot_max_draws,
            self.settings.slot_max_cost,
        )
