"""投运协同台的领域对象：从立项资金到正式开放的连续档案。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# 竣工里程碑名称：投运期限自该里程碑验收时刻起算。
COMPLETION_MILESTONE = "completion"


class ClassType(str, Enum):
    """班型：全日托、半日托、计时托。"""

    FULL_DAY = "full_day"
    HALF_DAY = "half_day"
    HOURLY = "hourly"


class Role(str, Enum):
    """放行所需的三类互斥职责。"""

    CONSTRUCTION_ACCEPTOR = "construction_acceptor"  # 建设验收
    OPERATIONS_APPROVER = "operations_approver"  # 运营批准
    SAFETY_SUPERVISOR = "safety_supervisor"  # 安全监督


#: 放行必须集齐且不得由同一人兼任的三类职责。
RELEASE_ROLES: tuple[Role, ...] = (
    Role.CONSTRUCTION_ACCEPTOR,
    Role.OPERATIONS_APPROVER,
    Role.SAFETY_SUPERVISOR,
)


class RequirementKind(str, Enum):
    """服务单元放行的合规依赖项类别。"""

    FIRE_ACCEPTANCE = "fire_acceptance"  # 消防验收
    FOOD_SAFETY = "food_safety"  # 食品安全制度
    STAFF_QUALIFICATION = "staff_qualification"  # 保育人员资质
    STAFF_SHIFT = "staff_shift"  # 保育人员班次
    OPERATING_RULES = "operating_rules"  # 运营规章
    MEDICAL_AGREEMENT = "medical_agreement"  # 妇幼机构服务协议
    TRIAL_RUN = "trial_run"  # 试运行


class RequirementStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"


@dataclass(frozen=True)
class Requirement:
    """一项合规依赖及其核验证据；valid_until 为空表示长期有效。"""

    requirement_id: str
    kind: RequirementKind
    status: RequirementStatus = RequirementStatus.PENDING
    evidence_ref: str | None = None
    verified_by: str | None = None
    valid_until: datetime | None = None

    def is_effective(self, now: datetime) -> bool:
        if self.status is not RequirementStatus.VERIFIED:
            return False
        return self.valid_until is None or now < self.valid_until


@dataclass(frozen=True)
class Signature:
    """职责人签署，三者缺一或同人兼任均不得放行。"""

    role: Role
    signer: str
    signed_at: datetime


@dataclass(frozen=True)
class FacilityRoom:
    room_id: str
    name: str
    purpose: str


@dataclass(frozen=True)
class ServiceUnit:
    """服务单元（一个班型），声明其依赖的房间与合规项。"""

    unit_id: str
    class_type: ClassType
    room_ids: tuple[str, ...]
    capacity: int
    required_kinds: tuple[RequirementKind, ...]


@dataclass(frozen=True)
class Milestone:
    name: str
    accepted_at: datetime
    accepted_by: str
    evidence_ref: str


@dataclass(frozen=True)
class Remediation:
    """整改事项，挂在单个房间上，只冻结关联班型。"""

    remediation_id: str
    room_id: str
    opened_at: datetime
    evidence_ref: str
    closed_at: datetime | None = None
    close_evidence_ref: str | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True)
class FundingNode:
    """中央资金节点。"""

    node_id: str
    name: str
    amount: int
    planned_date: datetime
    confirmed_at: datetime | None = None


class AdjustmentKind(str, Enum):
    EXTENSION = "extension"  # 延期
    WAIVER = "waiver"  # 豁免


@dataclass(frozen=True)
class DeadlineAdjustment:
    """期限调整，延期与豁免均须保留依据。"""

    kind: AdjustmentKind
    days: int
    reason: str
    evidence_ref: str
    granted_by: str
    granted_at: datetime


@dataclass(frozen=True)
class Release:
    """放行记录，保留审批所依据的基线版本。"""

    unit_id: str
    granted_at: datetime
    baseline_version: int
    signatures: tuple[Signature, ...]


@dataclass
class ProjectArchive:
    """单个托育中心从立项到开放的连续档案。"""

    project_id: str
    city: str
    coverage_target: int = 0
    milestones: list[Milestone] = field(default_factory=list)
    funding_nodes: dict[str, FundingNode] = field(default_factory=dict)
    rooms: dict[str, FacilityRoom] = field(default_factory=dict)
    units: dict[str, ServiceUnit] = field(default_factory=dict)
    requirements: dict[str, Requirement] = field(default_factory=dict)
    remediations: dict[str, Remediation] = field(default_factory=dict)
    adjustments: list[DeadlineAdjustment] = field(default_factory=list)
    releases: dict[str, Release] = field(default_factory=dict)
    confirmed_enrollments: dict[str, int] = field(default_factory=dict)

    def requirement_of(self, kind: RequirementKind) -> Requirement | None:
        for requirement in self.requirements.values():
            if requirement.kind is kind:
                return requirement
        return None

    def completion_at(self) -> datetime | None:
        for milestone in self.milestones:
            if milestone.name == COMPLETION_MILESTONE:
                return milestone.accepted_at
        return None

    def open_remediations(self) -> list[Remediation]:
        return [item for item in self.remediations.values() if item.is_open]

    def open_remediation_rooms(self) -> set[str]:
        return {item.room_id for item in self.open_remediations()}
