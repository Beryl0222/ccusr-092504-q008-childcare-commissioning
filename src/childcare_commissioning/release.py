"""服务单元放行评估：依赖有效、职责分人签署，局部整改只冻结关联班型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .domain import (
    RELEASE_ROLES,
    ProjectArchive,
    Release,
    ServiceUnit,
    Signature,
)


class UnitState(str, Enum):
    NOT_RELEASED = "not_released"  # 未放行
    OPERATING = "operating"  # 运营中
    FROZEN = "frozen"  # 已放行但被整改或依赖失效冻结


class CenterStatus(str, Enum):
    """中心运营状态只能由各班型状态推导，不允许直接填报。"""

    NOT_OPERATING = "not_operating"
    PARTIALLY_OPERATING = "partially_operating"
    FULLY_OPERATING = "fully_operating"


@dataclass(frozen=True)
class ReleaseDecision:
    unit_id: str
    allowed: bool
    reasons: tuple[str, ...]


class ReleaseDenied(Exception):
    """放行条件不满足时抛出，携带稳定排序的原因编码。"""

    def __init__(self, decision: ReleaseDecision) -> None:
        super().__init__(f"放行被拒绝: {', '.join(decision.reasons)}")
        self.decision = decision


def evaluate_release(
    unit: ServiceUnit,
    archive: ProjectArchive,
    signatures: tuple[Signature, ...],
    now: datetime,
) -> ReleaseDecision:
    """只有依赖项全部有效且三类职责由不同人签署后才可放行。"""
    reasons: list[str] = []

    for kind in unit.required_kinds:
        requirement = archive.requirement_of(kind)
        if requirement is None:
            reasons.append(f"missing_requirement:{kind.value}")
        elif not requirement.is_effective(now):
            reasons.append(f"requirement_not_effective:{kind.value}")

    blocked_rooms = archive.open_remediation_rooms()
    for room_id in unit.room_ids:
        if room_id not in archive.rooms:
            reasons.append(f"unknown_room:{room_id}")
        elif room_id in blocked_rooms:
            reasons.append(f"room_under_remediation:{room_id}")

    by_role = {signature.role: signature for signature in signatures}
    for role in RELEASE_ROLES:
        if role not in by_role:
            reasons.append(f"missing_signature:{role.value}")
    signers = [by_role[role].signer for role in RELEASE_ROLES if role in by_role]
    if len(signers) != len(set(signers)):
        reasons.append("signature_role_conflict")

    return ReleaseDecision(unit.unit_id, not reasons, tuple(sorted(reasons)))


def grant_release(
    unit: ServiceUnit,
    archive: ProjectArchive,
    signatures: tuple[Signature, ...],
    baseline_version: int,
    now: datetime,
) -> Release:
    """评估通过则登记放行记录，否则抛出 ReleaseDenied。"""
    decision = evaluate_release(unit, archive, signatures, now)
    if not decision.allowed:
        raise ReleaseDenied(decision)
    release = Release(unit.unit_id, now, baseline_version, signatures)
    archive.releases[unit.unit_id] = release
    return release


def unit_state(unit: ServiceUnit, archive: ProjectArchive, now: datetime) -> UnitState:
    """已放行的班型在依赖失效或关联房间整改时转为冻结。"""
    if unit.unit_id not in archive.releases:
        return UnitState.NOT_RELEASED
    blocked_rooms = archive.open_remediation_rooms()
    if any(room_id in blocked_rooms for room_id in unit.room_ids):
        return UnitState.FROZEN
    for kind in unit.required_kinds:
        requirement = archive.requirement_of(kind)
        if requirement is None or not requirement.is_effective(now):
            return UnitState.FROZEN
    return UnitState.OPERATING


def center_status(archive: ProjectArchive, now: datetime) -> CenterStatus:
    """由班型状态推导中心状态，杜绝虚报整中心已运营。"""
    if not archive.units:
        return CenterStatus.NOT_OPERATING
    operating = [
        unit for unit in archive.units.values() if unit_state(unit, archive, now) is UnitState.OPERATING
    ]
    if not operating:
        return CenterStatus.NOT_OPERATING
    if len(operating) == len(archive.units):
        return CenterStatus.FULLY_OPERATING
    return CenterStatus.PARTIALLY_OPERATING
