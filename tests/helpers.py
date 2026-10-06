"""测试共用的档案构造工具。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from childcare_commissioning.domain import (  # noqa: E402
    ClassType,
    FacilityRoom,
    ProjectArchive,
    Requirement,
    RequirementKind,
    RequirementStatus,
    Role,
    ServiceUnit,
    Signature,
)

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=TZ)

ALL_KINDS = tuple(RequirementKind)


def make_archive() -> ProjectArchive:
    """三种班型齐备、依赖项全部核验在有效期内的档案。"""
    archive = ProjectArchive("p1", "示例市", coverage_target=100)
    archive.rooms["r1"] = FacilityRoom("r1", "一楼活动室", "保育")
    archive.rooms["r2"] = FacilityRoom("r2", "二楼睡眠室", "保育")
    archive.units["u1"] = ServiceUnit("u1", ClassType.FULL_DAY, ("r1",), 20, ALL_KINDS)
    archive.units["u2"] = ServiceUnit("u2", ClassType.HALF_DAY, ("r2",), 15, ALL_KINDS)
    archive.units["u3"] = ServiceUnit("u3", ClassType.HOURLY, ("r1", "r2"), 10, ALL_KINDS)
    for kind in ALL_KINDS:
        archive.requirements[f"req-{kind.value}"] = Requirement(
            f"req-{kind.value}",
            kind,
            RequirementStatus.VERIFIED,
            evidence_ref=f"ev-{kind.value}",
            verified_by="核验员丁",
        )
    return archive


def make_signatures(now: datetime = T0) -> tuple[Signature, ...]:
    """三类职责分人签署。"""
    return (
        Signature(Role.CONSTRUCTION_ACCEPTOR, "验收员甲", now),
        Signature(Role.OPERATIONS_APPROVER, "批准人乙", now),
        Signature(Role.SAFETY_SUPERVISOR, "监督员丙", now),
    )
