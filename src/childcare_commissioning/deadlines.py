"""中央资金节点与竣工后投运期限：全部按可注入时钟计算，调整保留依据。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .domain import AdjustmentKind, ProjectArchive

#: 竣工后投运期限的默认窗口。
DEFAULT_COMMISSIONING_WINDOW = timedelta(days=90)

#: 依赖项临期提醒的默认提前量。
DEFAULT_EXPIRING_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class DeadlineReport:
    """投运期限报告：延期顺延、豁免免责，依据全部留痕。"""

    completion_at: datetime | None
    due_at: datetime | None
    waived: bool
    overdue: bool
    remaining_days: int | None
    evidence_refs: tuple[str, ...]


def commissioning_report(
    archive: ProjectArchive,
    now: datetime,
    window: timedelta = DEFAULT_COMMISSIONING_WINDOW,
) -> DeadlineReport:
    completion_at = archive.completion_at()
    evidence = tuple(item.evidence_ref for item in archive.adjustments)
    waived = any(item.kind is AdjustmentKind.WAIVER for item in archive.adjustments)
    if completion_at is None:
        return DeadlineReport(None, None, waived, False, None, evidence)
    extension_days = sum(
        item.days for item in archive.adjustments if item.kind is AdjustmentKind.EXTENSION
    )
    due_at = completion_at + window + timedelta(days=extension_days)
    remaining = (due_at - now).days
    return DeadlineReport(completion_at, due_at, waived, (now > due_at and not waived), remaining, evidence)


@dataclass(frozen=True)
class FundingNodeReport:
    node_id: str
    name: str
    amount: int
    planned_date: datetime
    confirmed_at: datetime | None
    overdue: bool


def funding_report(archive: ProjectArchive, now: datetime) -> tuple[FundingNodeReport, ...]:
    """中央资金节点逐条列出，到期未确认即为逾期。"""
    reports = [
        FundingNodeReport(
            node.node_id,
            node.name,
            node.amount,
            node.planned_date,
            node.confirmed_at,
            node.confirmed_at is None and now > node.planned_date,
        )
        for node in archive.funding_nodes.values()
    ]
    return tuple(sorted(reports, key=lambda item: (item.planned_date, item.node_id)))


@dataclass(frozen=True)
class Reminder:
    """到期提醒，重启后由档案重新计算，不依赖运行中的定时器。"""

    kind: str
    ref_id: str
    due_at: datetime
    message: str


def due_reminders(
    archive: ProjectArchive,
    now: datetime,
    expiring_within: timedelta = DEFAULT_EXPIRING_WINDOW,
) -> tuple[Reminder, ...]:
    reminders: list[Reminder] = []

    deadline = commissioning_report(archive, now)
    if (
        deadline.due_at is not None
        and not deadline.waived
        and deadline.remaining_days is not None
        and deadline.remaining_days <= expiring_within.days
    ):
        reminders.append(
            Reminder(
                "commissioning_due",
                archive.project_id,
                deadline.due_at,
                "投运期限临近" if not deadline.overdue else "投运期限已逾期",
            )
        )

    for node in funding_report(archive, now):
        if node.overdue:
            reminders.append(
                Reminder("funding_overdue", node.node_id, node.planned_date, f"资金节点[{node.name}]逾期未确认")
            )

    for requirement in archive.requirements.values():
        if requirement.valid_until is None:
            continue
        if now <= requirement.valid_until <= now + expiring_within:
            reminders.append(
                Reminder(
                    "requirement_expiring",
                    requirement.requirement_id,
                    requirement.valid_until,
                    f"依赖项[{requirement.kind.value}]即将到期",
                )
            )
        elif requirement.valid_until < now:
            reminders.append(
                Reminder(
                    "requirement_expired",
                    requirement.requirement_id,
                    requirement.valid_until,
                    f"依赖项[{requirement.kind.value}]已过期",
                )
            )

    return tuple(sorted(reminders, key=lambda item: (item.due_at, item.kind, item.ref_id)))
