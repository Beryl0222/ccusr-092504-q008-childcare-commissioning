"""主管视图：城市覆盖的真实阶段，以及从开放班型到证据链的追溯。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .deadlines import commissioning_report
from .domain import COMPLETION_MILESTONE, ProjectArchive, RequirementKind
from .release import UnitState, unit_state


class CityStage(str, Enum):
    """投运阶段，由档案事实推导，不允许直接填报。"""

    INITIATED = "initiated"  # 已立项
    UNDER_CONSTRUCTION = "under_construction"  # 建设中
    PRE_OPEN = "pre_open"  # 已竣工待投运
    PARTIALLY_OPEN = "partially_open"  # 部分班型开放
    FULLY_OPEN = "fully_open"  # 全部班型开放


@dataclass(frozen=True)
class CityReport:
    project_id: str
    city: str
    stage: CityStage
    operating_capacity: int
    coverage_target: int
    coverage_ratio: float | None
    open_remediations: int
    commissioning_overdue: bool


def city_report(archive: ProjectArchive, now: datetime) -> CityReport:
    states = {unit.unit_id: unit_state(unit, archive, now) for unit in archive.units.values()}
    operating = sum(
        archive.units[unit_id].capacity
        for unit_id, state in states.items()
        if state is UnitState.OPERATING
    )
    if archive.units and states and all(state is UnitState.OPERATING for state in states.values()):
        stage = CityStage.FULLY_OPEN
    elif any(state is UnitState.OPERATING for state in states.values()):
        stage = CityStage.PARTIALLY_OPEN
    elif any(milestone.name == COMPLETION_MILESTONE for milestone in archive.milestones):
        stage = CityStage.PRE_OPEN
    elif archive.milestones:
        stage = CityStage.UNDER_CONSTRUCTION
    else:
        stage = CityStage.INITIATED
    ratio = (operating / archive.coverage_target) if archive.coverage_target else None
    return CityReport(
        archive.project_id,
        archive.city,
        stage,
        operating,
        archive.coverage_target,
        ratio,
        len(archive.open_remediations()),
        commissioning_report(archive, now).overdue,
    )


@dataclass(frozen=True)
class RequirementTrace:
    kind: str
    status: str
    evidence_ref: str | None
    verified_by: str | None
    valid_until: datetime | None


@dataclass(frozen=True)
class TraceBundle:
    """从任一开放班型追到设施、人员、制度和医疗保障的证据链。"""

    unit_id: str
    class_type: str
    state: UnitState
    rooms: tuple[str, ...]
    requirements: tuple[RequirementTrace, ...]
    release_signatures: tuple[tuple[str, str], ...]
    baseline_version: int | None
    medical_evidence_ref: str | None


def trace_unit(archive: ProjectArchive, unit_id: str, now: datetime) -> TraceBundle:
    unit = archive.units[unit_id]
    traces: list[RequirementTrace] = []
    medical_evidence: str | None = None
    for kind in unit.required_kinds:
        requirement = archive.requirement_of(kind)
        if requirement is None:
            traces.append(RequirementTrace(kind.value, "missing", None, None, None))
            continue
        traces.append(
            RequirementTrace(
                kind.value,
                requirement.status.value,
                requirement.evidence_ref,
                requirement.verified_by,
                requirement.valid_until,
            )
        )
        if kind is RequirementKind.MEDICAL_AGREEMENT:
            medical_evidence = requirement.evidence_ref
    release = archive.releases.get(unit_id)
    signatures = (
        tuple((item.role.value, item.signer) for item in release.signatures) if release else ()
    )
    return TraceBundle(
        unit_id,
        unit.class_type.value,
        unit_state(unit, archive, now),
        tuple(archive.rooms[room_id].name for room_id in unit.room_ids if room_id in archive.rooms),
        tuple(sorted(traces, key=lambda item: item.kind)),
        signatures,
        release.baseline_version if release else None,
        medical_evidence,
    )
