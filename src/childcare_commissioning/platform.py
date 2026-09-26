"""托育中心投运协同台。

把立项资金、建设里程碑、整改事项、设施房间、班型容量、人员资质、
运营规章、医疗合作、试运行与正式开放串成连续档案，并在其上提供：

- 服务单元放行门：依赖全部有效且职责人签署才可放行；
- 角色分离：建设验收、运营批准、安全监督由不同角色/人员完成；
- 局部冻结：房间整改只冻结关联班型，不虚报整中心运营；
- 注入时钟：中央资金节点与竣工后投运期限按同一时钟判定；
- 延期/豁免/整改全程留证；
- 需求隔离：社区与用人单位需求只进入容量规划，不接触儿童信息、不占名额；
- 基线并发：审批携带读取到的聚合版本，冲突显式失败；
- 材料去重与重启续办：重复材料只收一次，重启后继续复核与提醒；
- 主管视图：城市覆盖真实阶段，以及从开放班型回溯的证据链。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from .clock import Clock, SystemClock
from .store import EventStore, StoredEvent

# 三种托育服务班型
FULL_DAY = "FULL_DAY"  # 全日托
HALF_DAY = "HALF_DAY"  # 半日托
HOURLY = "HOURLY"  # 计时托
UNIT_CODES = (FULL_DAY, HALF_DAY, HOURLY)

# 放行审批的三种互斥角色
ROLE_CONSTRUCTION = "CONSTRUCTION_ACCEPTANCE"  # 建设验收
ROLE_OPERATIONS = "OPERATIONS_APPROVAL"  # 运营批准
ROLE_SAFETY = "SAFETY_SUPERVISION"  # 安全监督
RELEASE_ROLES = (ROLE_CONSTRUCTION, ROLE_OPERATIONS, ROLE_SAFETY)

# 关键里程碑
MILESTONE_FUNDING = "FUNDING_IN_PLACE"  # 中央资金到位
MILESTONE_COMPLETION = "MAIN_COMPLETION"  # 主体竣工（投运期限起算点）

# 关键合规要求/制度类别
REQUIREMENT_FIRE_ACCEPTANCE = "FIRE_ACCEPTANCE"  # 消防验收
REGULATION_FOOD_SAFETY = "FOOD_SAFETY"  # 食品安全制度


class DomainError(RuntimeError):
    """业务规则被违反。"""


def _parse(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise DomainError(f"时间必须带时区：{value}")
    return moment


def _daystamp(moment: datetime) -> str:
    return moment.date().isoformat()


@dataclass
class Blocker:
    code: str
    message: str


@dataclass
class GateReport:
    project_id: str
    unit: str
    frozen: bool
    blockers: list[Blocker] = field(default_factory=list)

    @property
    def dependencies_valid(self) -> bool:
        return not self.blockers and not self.frozen

    @property
    def ready_for_trial(self) -> bool:
        return self.dependencies_valid


@dataclass
class UnitProgress:
    project_id: str
    unit: str
    capacity: int
    status: str  # NOT_DECLARED / BLOCKED / FROZEN / READY / TRIAL / OPEN
    blockers: list[Blocker] = field(default_factory=list)


@dataclass
class CityProgress:
    city: str
    coverage_target: int
    projects: int
    open_capacity: int
    trial_capacity: int
    stage: str  # NOT_STARTED / BUILDING / TRIAL / OPENING / COVERED


class Platform:
    def __init__(self, store: EventStore, *, auto_resume: bool = True) -> None:
        self.store = store
        self.clock: Clock = store.clock
        # 聚合读模型
        self.projects: dict[str, dict[str, Any]] = {}
        self.rooms: dict[str, dict[str, Any]] = {}
        self.staff: dict[str, dict[str, Any]] = {}
        self.regulations: dict[str, dict[str, Any]] = {}
        self.medical: dict[str, dict[str, Any]] = {}
        self.units: dict[str, dict[str, Any]] = {}
        self.releases: dict[str, dict[str, Any]] = {}
        self.demands: list[dict[str, Any]] = []
        self.materials: dict[str, dict[str, Any]] = {}
        self.coverage_targets: dict[str, int] = {}
        self.reminders: list[dict[str, Any]] = []
        self._acks: dict[tuple[str, str], set[str]] = {}
        self._applied_ids: set[str] = set()
        for event in self.store.events():
            self._apply(event)
        if auto_resume and self.store.events():
            self.resume_pending_reviews()

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _unit_id(project_id: str, unit: str) -> str:
        return f"{project_id}:{unit}"

    @staticmethod
    def _release_id(project_id: str, unit: str) -> str:
        return f"{project_id}:{unit}:release"

    def _require_project(self, project_id: str) -> dict[str, Any]:
        project = self.projects.get(project_id)
        if project is None:
            raise DomainError(f"项目未立项：{project_id}")
        return project

    def _append(self, **kwargs) -> StoredEvent:
        # 内部命令追加到已有聚合流时默认以当前版本为基线；
        # 需要显式乐观并发的命令（如三方放行审批）由调用方传入 expected_version。
        if kwargs.get("expected_version") is None and self.store.version(kwargs["aggregate_id"]) > 0:
            kwargs["expected_version"] = self.store.version(kwargs["aggregate_id"])
        event = self.store.append(**kwargs)
        if event.event_id not in self._applied_ids:
            self._apply(event)
        return event

    # ------------------------------------------------------------------ 立项

    def register_project(
        self,
        project_id: str,
        *,
        city: str,
        name: str,
        funded_amount: int,
        funding_due: str,
        commissioning_window_days: int,
        owner: str,
    ) -> StoredEvent:
        """中央投资项目立项并登记资金节点与竣工后投运期限窗口。"""
        if project_id in self.projects:
            raise DomainError(f"项目已存在：{project_id}")
        if funded_amount <= 0:
            raise DomainError("中央投资金额必须为正数")
        if commissioning_window_days < 0:
            raise DomainError("投运期限窗口不能为负")
        _parse(funding_due)
        return self._append(
            event_id=f"fund:{project_id}",
            event_type="PROJECT_FUNDED",
            aggregate_type="construction_project",
            aggregate_id=project_id,
            summary=f"项目立项：{name}（{city}），中央资金节点 {funding_due}",
            payload={
                "city": city,
                "name": name,
                "funded_amount": funded_amount,
                "funding_due": funding_due,
                "commissioning_window_days": commissioning_window_days,
                "owner": owner,
            },
        )

    def set_coverage_target(self, city: str, seats: int) -> None:
        if seats < 0:
            raise DomainError("覆盖目标不能为负")
        self.coverage_targets[city] = seats

    # ------------------------------------------------------------------ 里程碑

    def accept_milestone(
        self,
        project_id: str,
        code: str,
        *,
        actor: str,
        evidence_material_id: str | None = None,
        expected_version: int | None = None,
    ) -> StoredEvent:
        self._require_project(project_id)
        if code not in (MILESTONE_FUNDING, MILESTONE_COMPLETION):
            raise DomainError(f"未登记的里程碑：{code}")
        return self._append(
            event_id=f"milestone:{project_id}:{code}",
            event_type="MILESTONE_ACCEPTED",
            aggregate_type="construction_project",
            aggregate_id=project_id,
            summary=f"里程碑验收通过：{code}（{actor}）",
            payload={
                "milestone": code,
                "actor": actor,
                "evidence_material_id": evidence_material_id,
            },
            expected_version=self.store.version(project_id)
            if expected_version is None
            else expected_version,
        )

    # ------------------------------------------------------------------ 房间/整改

    def register_room(
        self, project_id: str, room_id: str, *, name: str, owner: str
    ) -> StoredEvent:
        self._require_project(project_id)
        if room_id in self.rooms:
            raise DomainError(f"房间已登记：{room_id}")
        return self._append(
            event_id=f"room:{room_id}",
            event_type="ROOM_REGISTERED",
            aggregate_type="facility_room",
            aggregate_id=room_id,
            summary=f"登记设施房间：{name}",
            payload={"project_id": project_id, "name": name, "owner": owner},
        )

    def acknowledge_owner(
        self, aggregate_type: str, dependency_id: str, *, owner: str
    ) -> StoredEvent:
        """职责人对依赖项签署确认（OWNER_ACKNOWLEDGED）。"""
        known = {
            "facility_room": self.rooms,
            "service_unit": self.units,
            "staff_member": self.staff,
        }
        if aggregate_type not in known or dependency_id not in known[aggregate_type]:
            raise DomainError(f"待签署依赖不存在：{aggregate_type}/{dependency_id}")
        version = self.store.version(dependency_id)
        return self._append(
            event_id=f"ack:{dependency_id}:{version + 1}",
            event_type="OWNER_ACKNOWLEDGED",
            aggregate_type=aggregate_type,
            aggregate_id=dependency_id,
            summary=f"职责人签署：{owner}",
            payload={"owner": owner},
        )

    def open_remediation(
        self,
        room_id: str,
        *,
        issue: str,
        owner: str,
        due: str,
        material_id: str | None = None,
    ) -> StoredEvent:
        """局部房间整改：只冻结使用该房间的班型，不影响其他班型。"""
        room = self.rooms.get(room_id)
        if room is None:
            raise DomainError(f"房间未登记：{room_id}")
        if room["open_remediation"] is not None:
            raise DomainError(f"房间已有未关闭整改：{room_id}")
        _parse(due)
        return self._append(
            event_id=f"rem-open:{room_id}:{self.store.version(room_id) + 1}",
            event_type="REMEDIATION_OPENED",
            aggregate_type="facility_room",
            aggregate_id=room_id,
            summary=f"整改事项立案：{issue}",
            payload={"issue": issue, "owner": owner, "due": due, "material_id": material_id},
        )

    def close_remediation(
        self, room_id: str, *, actor: str, material_id: str | None = None
    ) -> StoredEvent:
        room = self.rooms.get(room_id)
        if room is None:
            raise DomainError(f"房间未登记：{room_id}")
        if room["open_remediation"] is None:
            raise DomainError(f"房间没有待关闭整改：{room_id}")
        return self._append(
            event_id=f"rem-close:{room_id}:{self.store.version(room_id) + 1}",
            event_type="REMEDIATION_CLOSED",
            aggregate_type="facility_room",
            aggregate_id=room_id,
            summary="整改事项关闭，房间恢复可用",
            payload={"actor": actor, "material_id": material_id},
        )

    # ------------------------------------------------------------------ 容量

    def declare_capacity(
        self,
        project_id: str,
        unit: str,
        *,
        capacity: int,
        rooms: Iterable[str],
        regulation_versions: Mapping[str, str],
        medical_version: str,
        owner: str,
    ) -> StoredEvent:
        """登记班型容量表及其引用的房间、制度版本、医疗协议版本。"""
        self._require_project(project_id)
        if unit not in UNIT_CODES:
            raise DomainError(f"未知班型：{unit}")
        if capacity <= 0:
            raise DomainError("班型容量必须为正数")
        rooms = list(rooms)
        if not rooms:
            raise DomainError("班型至少绑定一个房间")
        for room_id in rooms:
            room = self.rooms.get(room_id)
            if room is None or room["project_id"] != project_id:
                raise DomainError(f"班型引用了不属于本项目的房间：{room_id}")
        unit_id = self._unit_id(project_id, unit)
        if unit_id in self.units:
            raise DomainError(f"班型容量已登记：{unit_id}（变更需走新版本流程）")
        return self._append(
            event_id=f"capacity:{unit_id}",
            event_type="CAPACITY_DECLARED",
            aggregate_type="service_unit",
            aggregate_id=unit_id,
            summary=f"班型容量表：{unit}={capacity}，职责人 {owner}",
            payload={
                "project_id": project_id,
                "unit": unit,
                "capacity": capacity,
                "rooms": rooms,
                "regulation_versions": dict(regulation_versions),
                "medical_version": medical_version,
                "owner": owner,
            },
        )

    # ------------------------------------------------------------------ 人员

    def register_staff(
        self,
        project_id: str,
        staff_id: str,
        *,
        name: str,
        qualification: str,
        qualification_no: str,
        valid: bool = True,
        owner: str,
    ) -> StoredEvent:
        self._require_project(project_id)
        if staff_id in self.staff:
            raise DomainError(f"人员已登记：{staff_id}")
        return self._append(
            event_id=f"staff:{staff_id}",
            event_type="STAFF_QUALIFIED",
            aggregate_type="staff_member",
            aggregate_id=staff_id,
            summary=f"人员资质登记：{name}（{qualification}）",
            payload={
                "project_id": project_id,
                "name": name,
                "qualification": qualification,
                "qualification_no": qualification_no,
                "valid": bool(valid),
                "owner": owner,
            },
        )

    def revalidate_staff_qualification(
        self, staff_id: str, *, valid: bool, actor: str, reason: str
    ) -> StoredEvent:
        """对在岗人员资质进行复核（如证件到期/吊销）。"""
        person = self.staff.get(staff_id)
        if person is None:
            raise DomainError(f"人员未登记：{staff_id}")
        seq = sum(
            1 for e in self.store.events(staff_id) if e.event_type == "STAFF_QUALIFIED"
        ) + 1
        return self._append(
            event_id=f"staff-qual:{staff_id}:{seq}",
            event_type="STAFF_QUALIFIED",
            aggregate_type="staff_member",
            aggregate_id=staff_id,
            summary=f"人员资质复核：{person['name']} {'有效' if valid else '失效'}（{actor}）",
            payload={
                "project_id": person["project_id"],
                "name": person["name"],
                "qualification": person["qualification"],
                "qualification_no": person["qualification_no"],
                "valid": bool(valid),
                "owner": actor,
                "review_reason": reason,
                "seq": seq,
            },
        )

    def assign_shift(
        self, project_id: str, staff_id: str, unit: str, *, scheduler: str
    ) -> StoredEvent:
        self._require_project(project_id)
        person = self.staff.get(staff_id)
        if person is None or person["project_id"] != project_id:
            raise DomainError(f"人员不属于本项目：{staff_id}")
        if unit not in UNIT_CODES:
            raise DomainError(f"未知班型：{unit}")
        seq = sum(1 for e in self.store.events(staff_id) if e.event_type == "SHIFT_ASSIGNED") + 1
        return self._append(
            event_id=f"shift:{staff_id}:{unit}:{seq}",
            event_type="SHIFT_ASSIGNED",
            aggregate_type="staff_member",
            aggregate_id=staff_id,
            summary=f"班次安排：{person['name']} → {unit}（{scheduler}）",
            payload={"unit": unit, "scheduler": scheduler, "seq": seq},
        )

    def sign_regulation(
        self,
        project_id: str,
        regulation_id: str,
        *,
        kind: str,
        version: str,
        owner: str,
        material_id: str | None = None,
    ) -> StoredEvent:
        self._require_project(project_id)
        seq = (
            sum(
                1
                for e in self.store.events(regulation_id)
                if e.event_type == "REGULATION_SIGNED"
            )
            + 1
        )
        return self._append(
            event_id=f"reg:{regulation_id}:{seq}",
            event_type="REGULATION_SIGNED",
            aggregate_type="operating_regulation",
            aggregate_id=regulation_id,
            summary=f"运营规章签署：{kind}@{version}（{owner}）",
            payload={
                "project_id": project_id,
                "kind": kind,
                "doc_version": version,
                "owner": owner,
                "material_id": material_id,
                "seq": seq,
            },
        )

    def sign_medical_agreement(
        self,
        project_id: str,
        agreement_id: str,
        *,
        partner: str,
        version: str,
        owner: str,
        material_id: str | None = None,
    ) -> StoredEvent:
        """与妇幼保健机构签署的服务协议；同一协议可多次签署新版本。"""
        self._require_project(project_id)
        seq = (
            sum(
                1
                for e in self.store.events(agreement_id)
                if e.event_type == "MEDICAL_AGREEMENT_SIGNED"
            )
            + 1
        )
        return self._append(
            event_id=f"med:{agreement_id}:{seq}",
            event_type="MEDICAL_AGREEMENT_SIGNED",
            aggregate_type="medical_agreement",
            aggregate_id=agreement_id,
            summary=f"医疗合作协议签署：{partner}@{version}",
            payload={
                "project_id": project_id,
                "partner": partner,
                "doc_version": version,
                "owner": owner,
                "material_id": material_id,
                "seq": seq,
            },
        )

    def verify_compliance(
        self,
        project_id: str,
        requirement_id: str,
        *,
        kind: str,
        valid: bool,
        actor: str,
        material_id: str | None = None,
    ) -> StoredEvent:
        """通用合规核验，如消防验收。"""
        self._require_project(project_id)
        seq = sum(
            1
            for e in self.store.events(requirement_id)
            if e.event_type == "REQUIREMENT_VERIFIED"
        ) + 1
        return self._append(
            event_id=f"req:{requirement_id}:{seq}",
            event_type="REQUIREMENT_VERIFIED",
            aggregate_type="compliance_requirement",
            aggregate_id=requirement_id,
            summary=f"合规核验：{kind} {'通过' if valid else '不通过'}（{actor}）",
            payload={
                "project_id": project_id,
                "kind": kind,
                "valid": bool(valid),
                "actor": actor,
                "material_id": material_id,
                "seq": seq,
            },
        )

    # ------------------------------------------------------------------ 材料

    def submit_material(
        self, *, material_id: str, kind: str, fingerprint: str, submitted_by: str
    ) -> StoredEvent:
        """材料台账：同一指纹只收一次，重复提交留 MATERIAL_DEDUPED 依据。"""
        registry_id = f"material:{fingerprint}"
        existing = self.materials.get(registry_id)
        if existing is not None:
            seq = existing["count"] + 1
            return self._append(
                event_id=f"dedup:{fingerprint}:{seq}",
                event_type="MATERIAL_DEDUPED",
                aggregate_type="material_registry",
                aggregate_id=registry_id,
                summary=f"重复材料拒收：{kind}（{submitted_by}）",
                payload={
                    "kind": kind,
                    "fingerprint": fingerprint,
                    "submitted_by": submitted_by,
                    "duplicate": True,
                    "original_material_id": existing["first_material_id"],
                    "seq": seq,
                },
            )
        return self._append(
            event_id=f"material-first:{fingerprint}",
            event_type="MATERIAL_DEDUPED",
            aggregate_type="material_registry",
            aggregate_id=registry_id,
            summary=f"材料首次收录：{kind}",
            payload={
                "kind": kind,
                "fingerprint": fingerprint,
                "submitted_by": submitted_by,
                "duplicate": False,
                "original_material_id": material_id,
                "seq": 1,
            },
        )

    # ------------------------------------------------------------------ 需求

    def record_community_demand(
        self,
        project_id: str,
        *,
        source: str,
        seats: int,
        age_band: str,
        child_name: str | None = None,
        contact: str | None = None,
    ) -> StoredEvent:
        """社区/用人单位需求登记。

        需求仅用于容量规划：协同台不获得任何儿童信息——事件载荷只保留来源、
        年龄段与席位聚合数；传入儿童姓名或联系方式会被直接拒绝。需求也不会
        占用任何班型已确认名额。
        """
        self._require_project(project_id)
        if source not in ("COMMUNITY", "EMPLOYER"):
            raise DomainError("需求来源必须是 COMMUNITY 或 EMPLOYER")
        if seats <= 0:
            raise DomainError("需求席位数必须为正数")
        if child_name or contact:
            raise DomainError("需求通道禁止收集儿童姓名或联系方式等个人信息")
        if not str(age_band).strip():
            raise DomainError("年龄段不能为空")
        seq = len(self.demands) + 1
        return self._append(
            event_id=f"demand:{project_id}:{seq}",
            event_type="DEMAND_RECORDED",
            aggregate_type="demand_intake",
            aggregate_id=f"{project_id}:demand:{seq}",
            summary=f"收到{'社区' if source == 'COMMUNITY' else '用人单位'}需求 {seats} 席（{age_band}）",
            payload={
                "project_id": project_id,
                "source": source,
                "seats": seats,
                "age_band": age_band,
                "seq": seq,
            },
        )

    # ------------------------------------------------------------------ 豁免/延期

    def grant_exemption(
        self,
        *,
        target_aggregate: str,
        reason: str,
        approver: str,
        material_id: str | None = None,
        expires_at: str | None = None,
    ) -> StoredEvent:
        """对单项依赖豁免（如某房间的当前整改项），不豁免整中心；可设到期时间。

        房间豁免绑定其当前在办整改实例，整改关闭后再立案的新整改不继承豁免。
        """
        if expires_at is not None:
            _parse(expires_at)
        anchor = None
        room = self.rooms.get(target_aggregate)
        if room is not None:
            active = room["open_remediation"]
            if active is None:
                raise DomainError("房间豁免必须针对一项在办整改")
            anchor = active["opened_event_id"]
        seq = sum(
            1
            for e in self.store.events(target_aggregate)
            if e.event_type == "EXEMPTION_GRANTED"
        ) + 1
        return self._append(
            event_id=f"exempt:{target_aggregate}:{seq}",
            event_type="EXEMPTION_GRANTED",
            aggregate_type=self._aggregate_kind(target_aggregate),
            aggregate_id=target_aggregate,
            summary=f"豁免批准：{reason}（{approver}）",
            payload={
                "reason": reason,
                "approver": approver,
                "material_id": material_id,
                "expires_at": expires_at,
                "remediation_anchor": anchor,
                "seq": seq,
            },
        )

    def _aggregate_kind(self, aggregate_id: str) -> str:
        if aggregate_id in self.rooms:
            return "facility_room"
        if aggregate_id in self.staff:
            return "staff_member"
        if aggregate_id in self.units:
            return "service_unit"
        if aggregate_id in self.regulations:
            return "operating_regulation"
        if aggregate_id in self.medical:
            return "medical_agreement"
        return "compliance_requirement"

    def extend_commissioning_deadline(
        self,
        project_id: str,
        *,
        new_deadline: str,
        reason: str,
        actor: str,
        material_id: str | None = None,
    ) -> StoredEvent:
        """竣工后投运期限延期，必须给出依据。"""
        project = self._require_project(project_id)
        parsed = _parse(new_deadline)
        if parsed <= _parse(project["commissioning_due"]):
            raise DomainError("延期后的期限必须晚于当前期限")
        seq = sum(
            1
            for e in self.store.events(project_id)
            if e.event_type == "DEADLINE_EXTENDED"
        ) + 1
        return self._append(
            event_id=f"extend:{project_id}:{seq}",
            event_type="DEADLINE_EXTENDED",
            aggregate_type="construction_project",
            aggregate_id=project_id,
            summary=f"投运期限延期至 {new_deadline}：{reason}",
            payload={
                "new_deadline": new_deadline,
                "reason": reason,
                "actor": actor,
                "material_id": material_id,
                "seq": seq,
            },
        )

    # ------------------------------------------------------------------ 放行门

    def _room_remediation_exempted(self, room_id: str) -> bool:
        """豁免仅在绑定当前在办整改实例且未到期时成立。"""
        room = self.rooms[room_id]
        active = room["open_remediation"]
        if active is None:
            return False
        anchor = active["opened_event_id"]
        for event in self.store.events(room_id):
            if event.event_type != "EXEMPTION_GRANTED":
                continue
            if event.payload.get("remediation_anchor") != anchor:
                continue
            expires_at = event.payload.get("expires_at")
            if expires_at is None or _parse(expires_at) > self.clock.now():
                return True
        return False

    def evaluate_gate(self, project_id: str, unit: str) -> GateReport:
        """不放行任何事件，只给出班型当前能否放行的结论与阻断项。"""
        project = self._require_project(project_id)
        blockers: list[Blocker] = []
        unit_id = self._unit_id(project_id, unit)
        unit_view = self.units.get(unit_id)

        if MILESTONE_FUNDING not in project["milestones"]:
            blockers.append(Blocker("FUNDING_NOT_IN_PLACE", "中央资金节点未完成"))
        if MILESTONE_COMPLETION not in project["milestones"]:
            blockers.append(Blocker("COMPLETION_NOT_ACCEPTED", "主体竣工未验收"))

        if unit_view is None:
            blockers.append(Blocker("CAPACITY_NOT_DECLARED", "班型容量表未登记"))
            return GateReport(project_id, unit, frozen=False, blockers=blockers)

        frozen_rooms: list[str] = []
        for room_id in unit_view["rooms"]:
            room = self.rooms[room_id]
            if room["open_remediation"] is not None:
                if self._room_remediation_exempted(room_id):
                    pass  # 该整改实例已获未到期豁免，暂不冻结
                else:
                    frozen_rooms.append(room_id)
                    blockers.append(
                        Blocker(
                            "ROOM_UNDER_REMEDIATION",
                            f"房间 {room_id} 整改未关闭，班型冻结（局部冻结）",
                        )
                    )
            if not self._acks.get(("facility_room", room_id)):
                blockers.append(
                    Blocker("ROOM_OWNER_UNSIGNED", f"房间 {room_id} 职责人未签署")
                )

        if not self._acks.get(("service_unit", unit_id)):
            blockers.append(Blocker("UNIT_OWNER_UNSIGNED", "班型职责人未签署容量表"))

        # 消防验收（项目级，最新结论必须有效或被豁免）
        fire_ok = self._latest_compliance_valid(project_id, REQUIREMENT_FIRE_ACCEPTANCE)
        if not fire_ok:
            blockers.append(Blocker("FIRE_ACCEPTANCE_MISSING", "消防验收未通过"))

        # 食品安全制度（班型引用版本必须与当前签署版本一致）
        food_version = unit_view["regulation_versions"].get(REGULATION_FOOD_SAFETY)
        current_food = self._current_regulation(project_id, REGULATION_FOOD_SAFETY)
        if current_food is None:
            blockers.append(Blocker("FOOD_SAFETY_MISSING", "食品安全制度未签署"))
        elif food_version != current_food:
            blockers.append(
                Blocker(
                    "REGULATION_VERSION_DRIFT",
                    f"食品安全制度版本漂移：容量表引用 {food_version}，当前 {current_food}",
                )
            )

        # 医疗合作协议版本一致且有效
        current_medical = self._current_medical(project_id)
        if current_medical is None:
            blockers.append(Blocker("MEDICAL_AGREEMENT_MISSING", "妇幼机构服务协议未签署"))
        elif unit_view["medical_version"] != current_medical:
            blockers.append(
                Blocker(
                    "MEDICAL_VERSION_DRIFT",
                    f"医疗协议版本漂移：引用 {unit_view['medical_version']}，当前 {current_medical}",
                )
            )

        # 人员：班型有足够持证人员且已排班
        assigned = [
            s for s in self.staff.values() if s["project_id"] == project_id and unit in s["shifts"]
        ]
        qualified = [s for s in assigned if s["valid"]]
        if not qualified:
            blockers.append(Blocker("STAFFING_MISSING", f"班型 {unit} 无持证到岗人员排班"))
        elif len(qualified) < project.get("min_staff_per_unit", 1):
            blockers.append(
                Blocker(
                    "STAFFING_INSUFFICIENT",
                    f"班型 {unit} 排班人数不足：{len(qualified)}/{project['min_staff_per_unit']}",
                )
            )

        return GateReport(project_id, unit, frozen=bool(frozen_rooms), blockers=blockers)

    def _latest_compliance_valid(self, project_id: str, kind: str) -> bool:
        # 同一合规类别可能在不同核验单上多次复验，取全局流中最后一次结论。
        latest = None
        for event in self.store.events():
            if (
                event.event_type == "REQUIREMENT_VERIFIED"
                and event.payload.get("project_id") == project_id
                and event.payload.get("kind") == kind
            ):
                latest = event
        return bool(latest and latest.payload["valid"])

    def _current_regulation(self, project_id: str, kind: str) -> str | None:
        version = None
        for event in self.store.events():
            if (
                event.event_type == "REGULATION_SIGNED"
                and event.payload.get("project_id") == project_id
                and event.payload.get("kind") == kind
            ):
                version = event.payload["doc_version"]
        return version

    def _current_medical(self, project_id: str) -> str | None:
        version = None
        for event in self.store.events():
            if (
                event.event_type == "MEDICAL_AGREEMENT_SIGNED"
                and event.payload.get("project_id") == project_id
            ):
                version = event.payload["doc_version"]
        return version

    # ------------------------------------------------------------------ 试运行/开放

    def open_trial_run(
        self, project_id: str, unit: str, *, min_trial_days: int = 0
    ) -> StoredEvent:
        """依赖全部有效且职责人签署后进入试运行；试运行不计入正式覆盖。"""
        report = self.evaluate_gate(project_id, unit)
        if not report.ready_for_trial:
            raise DomainError("; ".join(b.message for b in report.blockers) or "班型处于冻结")
        unit_id = self._unit_id(project_id, unit)
        release_id = self._release_id(project_id, unit)
        if self.releases.get(release_id, {}).get("trial_opened_at"):
            raise DomainError("该班型已进入试运行")
        return self._append(
            event_id=f"trial:{unit_id}",
            event_type="TRIAL_RUN_OPENED",
            aggregate_type="operating_release",
            aggregate_id=release_id,
            summary=f"班型 {unit} 启动试运行（观察期 {min_trial_days} 天）",
            payload={"unit": unit, "min_trial_days": min_trial_days},
        )

    def approve_release(
        self,
        project_id: str,
        unit: str,
        *,
        role: str,
        actor: str,
        expected_version: int | None = None,
        material_id: str | None = None,
    ) -> StoredEvent:
        """三方审批：建设验收、运营批准、安全监督必须由不同角色/人员完成。

        并发审批携带基线版本 expected_version；基线过期时抛
        ConcurrentModificationError，调用方须重读后再提交。
        """
        if role not in RELEASE_ROLES:
            raise DomainError(f"未知审批角色：{role}")
        release_id = self._release_id(project_id, unit)
        state = self.releases.get(release_id)
        for existing_role, existing_actor in (state or {}).get("approvals", {}).items():
            if existing_role != role and existing_actor == actor:
                raise DomainError(
                    f"{actor} 已承担 {existing_role}，不能再承担 {role}：三角色必须分离"
                )
        if state and role in state["approvals"]:
            raise DomainError(f"{role} 已完成审批")
        seq = len(state["approvals"]) + 1 if state else 1
        return self._append(
            event_id=f"approve:{release_id}:{role}",
            event_type="RELEASE_APPROVED",
            aggregate_type="operating_release",
            aggregate_id=release_id,
            summary=f"放行审批 {role}：{actor}",
            payload={
                "project_id": project_id,
                "unit": unit,
                "role": role,
                "actor": actor,
                "material_id": material_id,
                "seq": seq,
            },
            expected_version=self.store.version(release_id)
            if expected_version is None
            else expected_version,
        )

    def formally_open(self, project_id: str, unit: str) -> StoredEvent:
        """正式开放：放行门通过 + 试运行观察期满 + 三角色审批齐备。"""
        report = self.evaluate_gate(project_id, unit)
        if not report.dependencies_valid:
            raise DomainError("; ".join(b.message for b in report.blockers) or "班型处于冻结")
        release_id = self._release_id(project_id, unit)
        state = self.releases.get(release_id)
        if not state or not state.get("trial_opened_at"):
            raise DomainError("未完成试运行，不得正式开放")
        opened_at = _parse(state["trial_opened_at"])
        required = opened_at + timedelta(days=state.get("min_trial_days", 0))
        if self.clock.now() < required:
            raise DomainError(
                f"试运行观察期未满：最早 {required.isoformat()} 可正式开放"
            )
        roles = set(state["approvals"])
        missing = [r for r in RELEASE_ROLES if r not in roles]
        if missing:
            raise DomainError(f"放行审批角色不全，缺少：{', '.join(missing)}")
        if state.get("released"):
            raise DomainError("该班型已正式开放")
        return self._append(
            event_id=f"release:{self._unit_id(project_id, unit)}",
            event_type="SERVICE_RELEASED",
            aggregate_type="operating_release",
            aggregate_id=release_id,
            summary=f"班型 {unit} 正式开放",
            payload={"unit": unit},
        )

    # ------------------------------------------------------------------ 续办/提醒

    def resume_pending_reviews(self) -> list[StoredEvent]:
        """重启后继续未完成的复核：对每个在办整改留下 REVIEW_RESUMED 依据。

        事件标识含房间聚合当前版本，重放幂等；整改进展后再次重启会续记。
        """
        resumed: list[StoredEvent] = []
        for room_id, room in self.rooms.items():
            if room["open_remediation"] is None:
                continue
            # 以整改立案事件为稳定锚点：该整改关闭前无论重启多少次只续记一次。
            anchor = room["open_remediation"]["opened_event_id"]
            event_id = f"resume:remediation:{anchor}"
            if any(e.event_id == event_id for e in self.store.events()):
                continue
            resumed.append(
                self._append(
                    event_id=event_id,
                    event_type="REVIEW_RESUMED",
                    aggregate_type="facility_room",
                    aggregate_id=room_id,
                    summary="系统重启，继续未完成的整改复核",
                    payload={"issue": room["open_remediation"]["issue"]},
                )
            )
        return resumed

    def run_due_reviews(self) -> list[StoredEvent]:
        """按注入时钟扫描到期事项并提醒；同一天同一事项只提醒一次。"""
        now = self.clock.now()
        day = _daystamp(now)
        raised: list[StoredEvent] = []

        for project_id, project in self.projects.items():
            if MILESTONE_FUNDING not in project["milestones"]:
                due = _parse(project["funding_due"])
                if now > due:
                    raised.append(
                        self._reminder(
                            f"reminder:{project_id}:funding:{day}",
                            "construction_project",
                            project_id,
                            f"中央资金节点已逾期（{project['funding_due']}）",
                        )
                    )
            if MILESTONE_COMPLETION in project["milestones"] and not self._project_fully_open(
                project_id
            ):
                if now > _parse(project["commissioning_due"]):
                    raised.append(
                        self._reminder(
                            f"reminder:{project_id}:commissioning:{day}",
                            "construction_project",
                            project_id,
                            f"竣工后投运期限已逾期（{project['commissioning_due']}）",
                        )
                    )

        for room_id, room in self.rooms.items():
            active = room["open_remediation"]
            if active and now > _parse(active["due"]):
                raised.append(
                    self._reminder(
                        f"reminder:{room_id}:remediation:{day}",
                        "facility_room",
                        room_id,
                        f"房间整改已逾期：{active['issue']}（应于 {active['due']} 完成）",
                    )
                )

        for aggregate_id in list(self.rooms) + list(self.units):
            room = self.rooms.get(aggregate_id)
            for event in self.store.events(aggregate_id):
                if event.event_type != "EXEMPTION_GRANTED":
                    continue
                # 房间豁免随整改关闭而失效，只对仍在办的整改提醒豁免到期。
                if room is not None:
                    active = room["open_remediation"]
                    if active is None or event.payload.get("remediation_anchor") != active["opened_event_id"]:
                        continue
                expires_at = event.payload.get("expires_at")
                if expires_at and _parse(expires_at) <= now:
                    raised.append(
                        self._reminder(
                            f"reminder:{aggregate_id}:exemption:{event.payload['seq']}:{day}",
                            event.aggregate_type,
                            aggregate_id,
                            f"豁免已到期：{event.payload['reason']}",
                        )
                    )
        return [e for e in raised if e is not None]

    def _reminder(
        self, event_id: str, aggregate_type: str, aggregate_id: str, message: str
    ) -> StoredEvent | None:
        if any(e.event_id == event_id for e in self.store.events()):
            return None
        return self._append(
            event_id=event_id,
            event_type="REMINDER_RAISED",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            summary=message,
            payload={"message": message},
        )

    # ------------------------------------------------------------------ 主管视图

    def unit_progress(self, project_id: str, unit: str) -> UnitProgress:
        unit_id = self._unit_id(project_id, unit)
        unit_view = self.units.get(unit_id)
        capacity = unit_view["capacity"] if unit_view else 0
        release = self.releases.get(self._release_id(project_id, unit), {})
        if release.get("released"):
            report = self.evaluate_gate(project_id, unit)
            if report.frozen:
                status = "FROZEN"
            elif report.blockers:
                status = "BLOCKED"  # 开放后依赖失效（如制度版本漂移）
            else:
                status = "OPEN"
        elif release.get("trial_opened_at"):
            report = self.evaluate_gate(project_id, unit)
            if report.frozen:
                status = "FROZEN"
            elif report.blockers:
                status = "BLOCKED"  # 试运行期间依赖失效，暂停转正
            else:
                status = "TRIAL"
        elif unit_view is None:
            status = "NOT_DECLARED"
        else:
            report = self.evaluate_gate(project_id, unit)
            if report.frozen:
                status = "FROZEN"
            elif report.blockers:
                status = "BLOCKED"
            else:
                status = "READY"
        report = self.evaluate_gate(project_id, unit)
        return UnitProgress(project_id, unit, capacity, status, report.blockers)

    def _project_fully_open(self, project_id: str) -> bool:
        """整中心真实运营：每个已登记班型都正式开放且无冻结，且至少有一个班型。"""
        unit_ids = [u for u, v in self.units.items() if v["project_id"] == project_id]
        if not unit_ids:
            return False
        for unit_id in unit_ids:
            unit = unit_id.split(":", 1)[1]
            progress = self.unit_progress(project_id, unit)
            if progress.status != "OPEN":
                return False
        return True

    def center_status(self, project_id: str) -> dict[str, Any]:
        self._require_project(project_id)
        units = [
            self.unit_progress(project_id, unit)
            for unit in UNIT_CODES
            if self._unit_id(project_id, unit) in self.units
        ]
        return {
            "project_id": project_id,
            "units": [
                {"unit": p.unit, "status": p.status, "capacity": p.capacity} for p in units
            ],
            "fully_operational": self._project_fully_open(project_id),
        }

    def capacity_plan(self, project_id: str) -> dict[str, Any]:
        """容量规划视图：需求只输出分来源/分龄聚合，不收集儿童或联系人信息。"""
        self._require_project(project_id)
        buckets: dict[tuple[str, str], int] = {}
        for demand in self.demands:
            if demand["project_id"] != project_id:
                continue
            key = (demand["source"], demand["age_band"])
            buckets[key] = buckets.get(key, 0) + demand["seats"]
        return {
            "declared_capacity": {
                view["unit"]: view["capacity"]
                for view in self.units.values()
                if view["project_id"] == project_id
            },
            "demand_buckets": [
                {"source": source, "age_band": age, "seats": seats}
                for (source, age), seats in sorted(buckets.items())
            ],
            # 需求从不预留、抢占已确认名额
            "confirmed_reservations_from_demand": 0,
            # 协同台的需求通道在入口即拒收这些字段
            "personal_fields_rejected_at_intake": ["contact", "child_name"],
        }

    def city_dashboard(self) -> list[CityProgress]:
        rows: list[CityProgress] = []
        cities = {p["city"] for p in self.projects.values()} | set(self.coverage_targets)
        for city in sorted(cities):
            project_ids = [pid for pid, p in self.projects.items() if p["city"] == city]
            open_cap = trial_cap = 0
            stage = "NOT_STARTED"
            for pid in project_ids:
                for unit_id, view in self.units.items():
                    if view["project_id"] != pid:
                        continue
                    unit = unit_id.split(":", 1)[1]
                    progress = self.unit_progress(pid, unit)
                    if progress.status == "OPEN":
                        open_cap += view["capacity"]
                    elif progress.status == "TRIAL":
                        trial_cap += view["capacity"]
            target = self.coverage_targets.get(city, 0)
            if open_cap > 0:
                stage = "COVERED" if target == 0 or open_cap >= target else "OPENING"
            elif trial_cap > 0:
                stage = "TRIAL"
            elif project_ids:
                stage = "BUILDING"
            rows.append(
                CityProgress(
                    city=city,
                    coverage_target=target,
                    projects=len(project_ids),
                    open_capacity=open_cap,
                    trial_capacity=trial_cap,
                    stage=stage,
                )
            )
        return rows

    def evidence_chain(self, project_id: str, unit: str) -> dict[str, Any]:
        """从任一开放班型追到设施、人员、制度、医疗保障证据。"""
        unit_id = self._unit_id(project_id, unit)
        view = self.units.get(unit_id)
        if view is None:
            raise DomainError(f"班型未登记：{unit_id}")
        release_id = self._release_id(project_id, unit)
        room_evidence = []
        for room_id in view["rooms"]:
            room_evidence.append(
                {
                    "room_id": room_id,
                    "register_event": f"room:{room_id}",
                    "owner_signoffs": sorted(self._acks.get(("facility_room", room_id), set())),
                    "remediation_history": [
                        {
                            "event_id": e.event_id,
                            "type": e.event_type,
                            "summary": e.summary,
                        }
                        for e in self.store.events(room_id)
                        if e.event_type in ("REMEDIATION_OPENED", "REMEDIATION_CLOSED")
                    ],
                }
            )
        staff_events = []
        for person in self.staff.values():
            if person["project_id"] != project_id or unit not in person["shifts"]:
                continue
            staff_events.append(
                {
                    "staff_id": person["staff_id"],
                    "qualification": person["qualification"],
                    "qualification_no": person["qualification_no"],
                    "valid": person["valid"],
                    "qualification_event": f"staff:{person['staff_id']}",
                    "shift_events": [
                        e.event_id
                        for e in self.store.events(person["staff_id"])
                        if e.event_type == "SHIFT_ASSIGNED" and e.payload.get("unit") == unit
                    ],
                }
            )
        regulation_events = [
            {"event_id": e.event_id, "kind": e.payload["kind"], "version": e.payload["doc_version"]}
            for e in self.store.events()
            if e.event_type == "REGULATION_SIGNED" and e.payload.get("project_id") == project_id
        ]
        medical_events = [
            {"event_id": e.event_id, "partner": e.payload["partner"], "version": e.payload["doc_version"]}
            for e in self.store.events()
            if e.event_type == "MEDICAL_AGREEMENT_SIGNED"
            and e.payload.get("project_id") == project_id
        ]
        compliance_events = [
            {"event_id": e.event_id, "kind": e.payload["kind"], "valid": e.payload["valid"]}
            for e in self.store.events()
            if e.event_type == "REQUIREMENT_VERIFIED"
            and e.payload.get("project_id") == project_id
        ]
        release = self.releases.get(release_id, {})
        return {
            "project_id": project_id,
            "unit": unit,
            "capacity_event": f"capacity:{unit_id}",
            "declared_capacity": view["capacity"],
            "unit_owner_signoffs": sorted(self._acks.get(("service_unit", unit_id), set())),
            "rooms": room_evidence,
            "staff": staff_events,
            "regulations": regulation_events,
            "medical_agreements": medical_events,
            "compliance": compliance_events,
            "trial_event": f"trial:{unit_id}" if release.get("trial_opened_at") else None,
            "release_approvals": [
                {"role": role, "actor": actor}
                for role, actor in sorted(release.get("approvals", {}).items())
            ],
            "released_event": f"release:{unit_id}" if release.get("released") else None,
        }

    # ------------------------------------------------------------------ 读模型

    def _apply(self, event: StoredEvent) -> None:
        if event.event_id in self._applied_ids:
            return
        self._applied_ids.add(event.event_id)
        p = event.payload
        etype = event.event_type
        if etype == "PROJECT_FUNDED":
            self.projects[event.aggregate_id] = {
                "project_id": event.aggregate_id,
                "city": p["city"],
                "name": p["name"],
                "funded_amount": p["funded_amount"],
                "funding_due": p["funding_due"],
                "commissioning_window_days": p["commissioning_window_days"],
                "milestones": set(),
                "min_staff_per_unit": 1,
                "min_trial_days": 0,
            }
        elif etype == "MILESTONE_ACCEPTED":
            project = self.projects[event.aggregate_id]
            project["milestones"].add(p["milestone"])
            if p["milestone"] == MILESTONE_COMPLETION and "completion_at" not in project:
                completion = _parse(event.occurred_at)
                project["completion_at"] = event.occurred_at
                project["commissioning_due"] = (
                    completion + timedelta(days=project["commissioning_window_days"])
                ).isoformat()
        elif etype == "ROOM_REGISTERED":
            self.rooms[event.aggregate_id] = {
                "room_id": event.aggregate_id,
                "project_id": p["project_id"],
                "name": p["name"],
                "owner": p["owner"],
                "open_remediation": None,
            }
        elif etype == "OWNER_ACKNOWLEDGED":
            self._acks.setdefault((event.aggregate_type, event.aggregate_id), set()).add(
                p["owner"]
            )
        elif etype == "REMEDIATION_OPENED":
            self.rooms[event.aggregate_id]["open_remediation"] = {
                "issue": p["issue"],
                "owner": p["owner"],
                "due": p["due"],
                "opened_event_id": event.event_id,
            }
        elif etype == "REMEDIATION_CLOSED":
            self.rooms[event.aggregate_id]["open_remediation"] = None
        elif etype == "CAPACITY_DECLARED":
            self.units[event.aggregate_id] = {
                "project_id": p["project_id"],
                "unit": p["unit"],
                "capacity": p["capacity"],
                "rooms": list(p["rooms"]),
                "regulation_versions": dict(p["regulation_versions"]),
                "medical_version": p["medical_version"],
                "owner": p["owner"],
            }
        elif etype == "STAFF_QUALIFIED":
            # 资质复核事件沿用同一聚合，须保留此前已形成的班次事实。
            previous_shifts = self.staff.get(event.aggregate_id, {}).get("shifts", set())
            self.staff[event.aggregate_id] = {
                "staff_id": event.aggregate_id,
                "project_id": p["project_id"],
                "name": p["name"],
                "qualification": p["qualification"],
                "qualification_no": p["qualification_no"],
                "valid": p["valid"],
                "shifts": set(previous_shifts),
            }
        elif etype == "SHIFT_ASSIGNED":
            self.staff[event.aggregate_id]["shifts"].add(p["unit"])
        elif etype == "REGULATION_SIGNED":
            self.regulations[event.aggregate_id] = {
                "project_id": p["project_id"],
                "kind": p["kind"],
                "doc_version": p["doc_version"],
                "owner": p["owner"],
            }
        elif etype == "MEDICAL_AGREEMENT_SIGNED":
            self.medical[event.aggregate_id] = {
                "project_id": p["project_id"],
                "partner": p["partner"],
                "doc_version": p["doc_version"],
                "owner": p["owner"],
            }
        elif etype == "DEMAND_RECORDED":
            self.demands.append(
                {
                    "project_id": p["project_id"],
                    "source": p["source"],
                    "seats": p["seats"],
                    "age_band": p["age_band"],
                }
            )
        elif etype == "MATERIAL_DEDUPED":
            registry = self.materials.setdefault(
                event.aggregate_id,
                {"first_material_id": p.get("original_material_id"), "kind": p["kind"], "count": 0},
            )
            registry["count"] = p["seq"]
            if not p.get("duplicate"):
                registry["first_material_id"] = p["original_material_id"]
        elif etype == "TRIAL_RUN_OPENED":
            state = self.releases.setdefault(
                event.aggregate_id, {"approvals": {}, "trial_opened_at": None, "released": False}
            )
            state["trial_opened_at"] = event.occurred_at
            state["min_trial_days"] = p.get("min_trial_days", 0)
        elif etype == "RELEASE_APPROVED":
            state = self.releases.setdefault(
                event.aggregate_id, {"approvals": {}, "trial_opened_at": None, "released": False}
            )
            state["approvals"][p["role"]] = p["actor"]
        elif etype == "SERVICE_RELEASED":
            state = self.releases.setdefault(
                event.aggregate_id, {"approvals": {}, "trial_opened_at": None, "released": False}
            )
            state["released"] = True
        elif etype == "DEADLINE_EXTENDED":
            project = self.projects[event.aggregate_id]
            project["commissioning_due"] = p["new_deadline"]
        elif etype in ("REMINDER_RAISED", "REVIEW_RESUMED", "EXEMPTION_GRANTED",
                       "REQUIREMENT_VERIFIED"):
            if etype == "REMINDER_RAISED":
                self.reminders.append({"event_id": event.event_id, "message": p["message"]})
