"""需求容量规划：社区与用人单位的需求只用于规划，不触碰儿童信息与已确认名额。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .contracts import ContractIssue
from .domain import ClassType, ProjectArchive
from .release import UnitState, unit_state

#: 需求登记中禁止出现的字段：需求方不得直接获得儿童及其监护人信息。
FORBIDDEN_DEMAND_FIELDS = (
    "child_name",
    "child_id",
    "child_id_number",
    "child_birthdate",
    "guardian_name",
    "guardian_phone",
)


@dataclass(frozen=True)
class DemandRecord:
    """一条匿名化的托育需求，只携带规划所需的规模信息。"""

    demand_id: str
    source: str  # community / employer
    class_type: ClassType
    headcount: int
    submitted_at: datetime


def validate_demand(payload: Mapping[str, Any]) -> list[ContractIssue]:
    """拒绝携带儿童信息的登记，保证需求侧与在托儿童数据隔离。"""
    issues: list[ContractIssue] = []
    for field in FORBIDDEN_DEMAND_FIELDS:
        if field in payload:
            issues.append(ContractIssue(field, "child_data_forbidden", "需求登记不得携带儿童或监护人信息"))
    for field in ("demand_id", "source"):
        if not isinstance(payload.get(field), str) or not payload.get(field, "").strip():
            issues.append(ContractIssue(field, "non_empty_string", "字段必须是非空字符串"))
    headcount = payload.get("headcount")
    if isinstance(headcount, bool) or not isinstance(headcount, int) or headcount < 1:
        issues.append(ContractIssue("headcount", "positive_integer", "需求人数必须是正整数"))
    class_type = payload.get("class_type")
    if class_type not in {item.value for item in ClassType}:
        issues.append(ContractIssue("class_type", "unsupported_value", "班型未在契约中登记"))
    return sorted(issues, key=lambda issue: (issue.field, issue.code))


@dataclass(frozen=True)
class CapacityPlan:
    """单个班型的供需对照；available 已扣除确认名额，需求只能看到缺口。"""

    class_type: ClassType
    demand_headcount: int
    operating_capacity: int
    confirmed: int
    available: int
    gap: int


def plan_capacity(archive: ProjectArchive, demands: tuple[DemandRecord, ...], now: datetime) -> tuple[CapacityPlan, ...]:
    """按班型汇总需求与可用容量；确认名额只减不增，规划结果不改动档案。"""
    plans: list[CapacityPlan] = []
    for class_type in ClassType:
        demand = sum(item.headcount for item in demands if item.class_type is class_type)
        capacity = 0
        confirmed = 0
        for unit in archive.units.values():
            if unit.class_type is not class_type:
                continue
            if unit_state(unit, archive, now) is UnitState.OPERATING:
                capacity += unit.capacity
                confirmed += archive.confirmed_enrollments.get(unit.unit_id, 0)
        available = max(0, capacity - confirmed)
        plans.append(
            CapacityPlan(class_type, demand, capacity, confirmed, available, max(0, demand - available))
        )
    return tuple(plans)
