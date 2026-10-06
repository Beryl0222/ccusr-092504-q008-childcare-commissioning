"""事件档案库：基线版本并发控制、材料幂等收取、重启后恢复待办。

事件以 JSONL 追加写入磁盘，重开档案库时逐条回放即可重建连续档案，
未完成的复核与到期提醒不依赖运行中的进程状态。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .clock import Clock
from .contracts import ContractIssue, validate_event
from .deadlines import Reminder, due_reminders
from .domain import (
    AdjustmentKind,
    ClassType,
    DeadlineAdjustment,
    FacilityRoom,
    FundingNode,
    Milestone,
    ProjectArchive,
    Release,
    Remediation,
    Requirement,
    RequirementKind,
    RequirementStatus,
    Role,
    ServiceUnit,
    Signature,
)
from .release import ReleaseDenied, evaluate_release

_DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


class ConcurrencyConflict(Exception):
    """基线版本已过期：他人在评估之后写入了相关事实，需重新复核。"""

    def __init__(self, aggregate_type: str, aggregate_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"{aggregate_type}/{aggregate_id} 基线版本冲突: 基于 v{expected} 的复核，当前已是 v{actual}"
        )
        self.aggregate_type = aggregate_type
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.actual = actual


class ContractViolation(Exception):
    """事件不满足交换契约时抛出。"""

    def __init__(self, issues: Iterable[ContractIssue]) -> None:
        self.issues = tuple(issues)
        summary = "; ".join(f"{issue.field}:{issue.code}" for issue in self.issues)
        super().__init__(f"事件违反契约: {summary}")


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


class Ledger:
    """追加式事件档案库，聚合版本即并发控制的基线。"""

    def __init__(self, path: str | Path, clock: Clock, schema: Mapping[str, Any] | None = None) -> None:
        self._path = Path(path)
        self._clock = clock
        if schema is None and _DEFAULT_SCHEMA_PATH.exists():
            schema = json.loads(_DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._schema = schema
        self._versions: dict[tuple[str, str], int] = {}
        self._archives: dict[str, ProjectArchive] = {}
        self._materials: dict[str, dict[str, Any]] = {}
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._replay(json.loads(line))

    # ---- 读取侧 ----

    def archive(self, project_id: str) -> ProjectArchive:
        return self._archives[project_id]

    def projects(self) -> tuple[str, ...]:
        return tuple(sorted(self._archives))

    def version_of(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions.get((aggregate_type, aggregate_id), 0)

    def baseline_version(self, project_id: str, unit_id: str) -> int:
        """审批基线：服务单元及其全部依赖流的版本之和。

        依赖流中任何一条推进（重新核验、房间整改、再次放行）都会使
        旧基线失效，在途审批必须基于新基线重新复核。
        """
        archive = self.archive(project_id)
        unit = archive.units[unit_id]
        streams = {("service_unit", unit_id), ("operating_release", unit_id)}
        streams.update(("remediation", room_id) for room_id in unit.room_ids)
        for kind in unit.required_kinds:
            requirement = archive.requirement_of(kind)
            if requirement is not None:
                streams.add(("compliance_requirement", requirement.requirement_id))
        return sum(self.version_of(*stream) for stream in streams)

    def pending_reviews(self, project_id: str) -> tuple[dict[str, Any], ...]:
        """已收取但对应依赖项尚未核验通过的材料，重启后依然可见。"""
        archive = self.archive(project_id)
        pending = [
            receipt
            for receipt in self._materials.values()
            if receipt["project_id"] == project_id
            and archive.requirements[receipt["requirement_id"]].status is not RequirementStatus.VERIFIED
        ]
        return tuple(sorted(pending, key=lambda item: (item["received_at"], item["material_id"])))

    def due_reminders(self, project_id: str) -> tuple[Reminder, ...]:
        return due_reminders(self.archive(project_id), self._clock.now())

    # ---- 写入侧 ----

    def append(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        expected_version: int,
        summary: str,
    ) -> dict[str, Any]:
        """追加事件；expected_version 与当前聚合版本不一致时拒绝写入。"""
        actual = self.version_of(aggregate_type, aggregate_id)
        if expected_version != actual:
            raise ConcurrencyConflict(aggregate_type, aggregate_id, expected_version, actual)
        event = {
            "event_id": f"{aggregate_type}:{aggregate_id}:v{actual + 1}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self._clock.now().isoformat(),
            "version": actual + 1,
            "summary": summary,
            "payload": dict(payload),
        }
        if self._schema is not None:
            issues = validate_event(event, self._schema)
            if issues:
                raise ContractViolation(issues)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._apply(event)
        return event

    def register_project(
        self,
        project_id: str,
        city: str,
        coverage_target: int,
        rooms: Iterable[Mapping[str, Any]] = (),
        funding_nodes: Iterable[Mapping[str, Any]] = (),
        requirements: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        payload = {
            "city": city,
            "coverage_target": coverage_target,
            "rooms": list(rooms),
            "funding_nodes": list(funding_nodes),
            "requirements": list(requirements),
        }
        return self.append(
            "PROJECT_REGISTERED", "construction_project", project_id, payload, 0, "立项登记"
        )

    def accept_milestone(self, project_id: str, name: str, accepted_by: str, evidence_ref: str) -> dict[str, Any]:
        payload = {"name": name, "accepted_by": accepted_by, "evidence_ref": evidence_ref}
        return self.append(
            "MILESTONE_ACCEPTED",
            "construction_project",
            project_id,
            payload,
            self.version_of("construction_project", project_id),
            f"里程碑[{name}]验收",
        )

    def confirm_funding_node(self, project_id: str, node_id: str) -> dict[str, Any]:
        return self.append(
            "FUNDING_NODE_CONFIRMED",
            "construction_project",
            project_id,
            {"node_id": node_id},
            self.version_of("construction_project", project_id),
            f"资金节点[{node_id}]确认",
        )

    def verify_requirement(
        self,
        project_id: str,
        requirement_id: str,
        evidence_ref: str,
        verified_by: str,
        valid_until: datetime | None = None,
    ) -> dict[str, Any]:
        requirement = self.archive(project_id).requirements[requirement_id]
        payload: dict[str, Any] = {
            "project_id": project_id,
            "kind": requirement.kind.value,
            "evidence_ref": evidence_ref,
            "verified_by": verified_by,
        }
        if valid_until is not None:
            payload["valid_until"] = valid_until.isoformat()
        return self.append(
            "REQUIREMENT_VERIFIED",
            "compliance_requirement",
            requirement_id,
            payload,
            self.version_of("compliance_requirement", requirement_id),
            f"依赖项[{requirement.kind.value}]核验通过",
        )

    def declare_capacity(
        self,
        project_id: str,
        unit_id: str,
        class_type: ClassType,
        room_ids: Iterable[str],
        capacity: int,
        required_kinds: Iterable[RequirementKind],
    ) -> dict[str, Any]:
        payload = {
            "project_id": project_id,
            "class_type": class_type.value,
            "room_ids": list(room_ids),
            "capacity": capacity,
            "required_kinds": [kind.value for kind in required_kinds],
        }
        return self.append(
            "CAPACITY_DECLARED",
            "service_unit",
            unit_id,
            payload,
            self.version_of("service_unit", unit_id),
            f"班型[{class_type.value}]容量申报",
        )

    def receive_material(
        self, project_id: str, requirement_id: str, material_id: str, content_hash: str
    ) -> dict[str, Any]:
        """收取复核材料；同一材料（按标识或内容哈希）只收一次。"""
        for receipt in self._materials.values():
            same_material = receipt["material_id"] == material_id
            same_content = (
                receipt["requirement_id"] == requirement_id and receipt["content_hash"] == content_hash
            )
            if same_material or same_content:
                return receipt
        event = self.append(
            "MATERIAL_RECEIVED",
            "compliance_requirement",
            requirement_id,
            {"project_id": project_id, "material_id": material_id, "content_hash": content_hash},
            self.version_of("compliance_requirement", requirement_id),
            f"材料[{material_id}]收取",
        )
        return self._materials[material_id]

    def open_remediation(self, project_id: str, remediation_id: str, room_id: str, evidence_ref: str) -> dict[str, Any]:
        return self.append(
            "REMEDIATION_OPENED",
            "remediation",
            room_id,
            {"project_id": project_id, "remediation_id": remediation_id, "evidence_ref": evidence_ref},
            self.version_of("remediation", room_id),
            f"房间[{room_id}]整改开工",
        )

    def close_remediation(self, project_id: str, remediation_id: str, evidence_ref: str) -> dict[str, Any]:
        room_id = self.archive(project_id).remediations[remediation_id].room_id
        return self.append(
            "REMEDIATION_CLOSED",
            "remediation",
            room_id,
            {"project_id": project_id, "remediation_id": remediation_id, "evidence_ref": evidence_ref},
            self.version_of("remediation", room_id),
            f"房间[{room_id}]整改销项",
        )

    def adjust_deadline(
        self,
        project_id: str,
        kind: AdjustmentKind,
        days: int,
        reason: str,
        evidence_ref: str,
        granted_by: str,
    ) -> dict[str, Any]:
        payload = {
            "kind": kind.value,
            "days": days,
            "reason": reason,
            "evidence_ref": evidence_ref,
            "granted_by": granted_by,
        }
        return self.append(
            "DEADLINE_ADJUSTED",
            "construction_project",
            project_id,
            payload,
            self.version_of("construction_project", project_id),
            f"投运期限{'延期' if kind is AdjustmentKind.EXTENSION else '豁免'}",
        )

    def confirm_enrollment(self, project_id: str, unit_id: str, count: int) -> dict[str, Any]:
        return self.append(
            "ENROLLMENT_CONFIRMED",
            "service_unit",
            unit_id,
            {"project_id": project_id, "count": count},
            self.version_of("service_unit", unit_id),
            f"班型[{unit_id}]确认名额 {count}",
        )

    def grant_release(
        self, project_id: str, unit_id: str, signatures: tuple[Signature, ...], baseline_version: int
    ) -> Release:
        """按基线版本放行：评估之后依赖流有变化则拒绝，需重新复核。"""
        archive = self.archive(project_id)
        unit = archive.units[unit_id]
        current = self.baseline_version(project_id, unit_id)
        if baseline_version != current:
            raise ConcurrencyConflict("operating_release", unit_id, baseline_version, current)
        now = self._clock.now()
        decision = evaluate_release(unit, archive, signatures, now)
        if not decision.allowed:
            raise ReleaseDenied(decision)
        payload = {
            "project_id": project_id,
            "baseline_version": baseline_version,
            "signatures": [
                {"role": item.role.value, "signer": item.signer, "signed_at": item.signed_at.isoformat()}
                for item in signatures
            ],
        }
        self.append(
            "SERVICE_RELEASED",
            "operating_release",
            unit_id,
            payload,
            self.version_of("operating_release", unit_id),
            f"班型[{unit.class_type.value}]放行",
        )
        return self.archive(project_id).releases[unit_id]

    # ---- 回放与投影 ----

    def _replay(self, event: dict[str, Any]) -> None:
        if self._schema is not None:
            issues = validate_event(event, self._schema)
            if issues:
                raise ContractViolation(issues)
        self._apply(event)

    def _apply(self, event: dict[str, Any]) -> None:
        key = (event["aggregate_type"], event["aggregate_id"])
        self._versions[key] = event["version"]
        self._project(event)

    def _material_event_receipt(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = event["payload"]
        receipt = {
            "material_id": payload["material_id"],
            "requirement_id": event["aggregate_id"],
            "project_id": payload["project_id"],
            "content_hash": payload["content_hash"],
            "received_at": event["occurred_at"],
            "receipt_no": event["event_id"],
        }
        self._materials[payload["material_id"]] = receipt
        return receipt

    def _project(self, event: dict[str, Any]) -> None:
        event_type = event["event_type"]
        payload = event["payload"]
        occurred_at = _parse(event["occurred_at"])

        if event_type == "PROJECT_REGISTERED":
            archive = ProjectArchive(
                project_id=event["aggregate_id"],
                city=payload["city"],
                coverage_target=payload["coverage_target"],
            )
            for room in payload.get("rooms", []):
                archive.rooms[room["room_id"]] = FacilityRoom(room["room_id"], room["name"], room["purpose"])
            for node in payload.get("funding_nodes", []):
                archive.funding_nodes[node["node_id"]] = FundingNode(
                    node["node_id"], node["name"], node["amount"], _parse(node["planned_date"])
                )
            for requirement in payload.get("requirements", []):
                archive.requirements[requirement["requirement_id"]] = Requirement(
                    requirement["requirement_id"], RequirementKind(requirement["kind"])
                )
            self._archives[archive.project_id] = archive
            return

        archive = self._archives[payload["project_id"]]

        if event_type == "MILESTONE_ACCEPTED":
            archive.milestones.append(
                Milestone(payload["name"], occurred_at, payload["accepted_by"], payload["evidence_ref"])
            )
        elif event_type == "FUNDING_NODE_CONFIRMED":
            node = archive.funding_nodes[payload["node_id"]]
            archive.funding_nodes[node.node_id] = replace(node, confirmed_at=occurred_at)
        elif event_type == "REQUIREMENT_VERIFIED":
            requirement = archive.requirements[event["aggregate_id"]]
            archive.requirements[requirement.requirement_id] = replace(
                requirement,
                status=RequirementStatus.VERIFIED,
                evidence_ref=payload["evidence_ref"],
                verified_by=payload["verified_by"],
                valid_until=_parse(payload["valid_until"]) if "valid_until" in payload else None,
            )
        elif event_type == "CAPACITY_DECLARED":
            archive.units[event["aggregate_id"]] = ServiceUnit(
                event["aggregate_id"],
                ClassType(payload["class_type"]),
                tuple(payload["room_ids"]),
                payload["capacity"],
                tuple(RequirementKind(kind) for kind in payload["required_kinds"]),
            )
        elif event_type == "MATERIAL_RECEIVED":
            self._material_event_receipt(event)
        elif event_type == "REMEDIATION_OPENED":
            archive.remediations[payload["remediation_id"]] = Remediation(
                payload["remediation_id"], event["aggregate_id"], occurred_at, payload["evidence_ref"]
            )
        elif event_type == "REMEDIATION_CLOSED":
            remediation = archive.remediations[payload["remediation_id"]]
            archive.remediations[remediation.remediation_id] = replace(
                remediation, closed_at=occurred_at, close_evidence_ref=payload["evidence_ref"]
            )
        elif event_type == "DEADLINE_ADJUSTED":
            archive.adjustments.append(
                DeadlineAdjustment(
                    AdjustmentKind(payload["kind"]),
                    payload["days"],
                    payload["reason"],
                    payload["evidence_ref"],
                    payload["granted_by"],
                    occurred_at,
                )
            )
        elif event_type == "SERVICE_RELEASED":
            archive.releases[event["aggregate_id"]] = Release(
                event["aggregate_id"],
                occurred_at,
                payload["baseline_version"],
                tuple(
                    Signature(Role(item["role"]), item["signer"], _parse(item["signed_at"]))
                    for item in payload["signatures"]
                ),
            )
        elif event_type == "ENROLLMENT_CONFIRMED":
            archive.confirmed_enrollments[event["aggregate_id"]] = payload["count"]
