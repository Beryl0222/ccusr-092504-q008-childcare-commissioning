from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from helpers import T0, make_archive

from childcare_commissioning.deadlines import commissioning_report, due_reminders, funding_report
from childcare_commissioning.domain import AdjustmentKind, DeadlineAdjustment, FundingNode, Milestone


def _archive_with_completion():
    archive = make_archive()
    archive.milestones.append(Milestone("completion", T0, "验收员甲", "ev-completion"))
    return archive


class CommissioningDeadlineTests(unittest.TestCase):
    def test_deadline_counts_from_completion_milestone(self) -> None:
        report = commissioning_report(_archive_with_completion(), T0 + timedelta(days=30))
        self.assertEqual(T0, report.completion_at)
        self.assertEqual(T0 + timedelta(days=90), report.due_at)
        self.assertEqual(60, report.remaining_days)
        self.assertFalse(report.overdue)
        self.assertFalse(report.waived)

    def test_overdue_after_window(self) -> None:
        report = commissioning_report(_archive_with_completion(), T0 + timedelta(days=91))
        self.assertTrue(report.overdue)

    def test_no_completion_no_deadline(self) -> None:
        report = commissioning_report(make_archive(), T0)
        self.assertIsNone(report.due_at)
        self.assertFalse(report.overdue)

    def test_extension_shifts_deadline_and_keeps_evidence(self) -> None:
        archive = _archive_with_completion()
        archive.adjustments.append(
            DeadlineAdjustment(AdjustmentKind.EXTENSION, 30, "消防复验待出证", "ev-ext-1", "市级专员", T0)
        )
        report = commissioning_report(archive, T0 + timedelta(days=100))
        self.assertEqual(T0 + timedelta(days=120), report.due_at)
        self.assertFalse(report.overdue)
        self.assertIn("ev-ext-1", report.evidence_refs)

    def test_waiver_exempts_overdue_with_evidence(self) -> None:
        archive = _archive_with_completion()
        archive.adjustments.append(
            DeadlineAdjustment(AdjustmentKind.WAIVER, 0, "政策调整暂缓投运", "ev-waiver-1", "市级专员", T0)
        )
        report = commissioning_report(archive, T0 + timedelta(days=200))
        self.assertTrue(report.waived)
        self.assertFalse(report.overdue)
        self.assertIn("ev-waiver-1", report.evidence_refs)


class FundingNodeTests(unittest.TestCase):
    def test_unconfirmed_node_past_planned_date_is_overdue(self) -> None:
        archive = make_archive()
        archive.funding_nodes["n1"] = FundingNode("n1", "首期拨付", 500, T0 + timedelta(days=10))
        (report,) = funding_report(archive, T0 + timedelta(days=11))
        self.assertTrue(report.overdue)
        confirmed = replace(archive.funding_nodes["n1"], confirmed_at=T0 + timedelta(days=9))
        archive.funding_nodes["n1"] = confirmed
        (report,) = funding_report(archive, T0 + timedelta(days=11))
        self.assertFalse(report.overdue)


class ReminderTests(unittest.TestCase):
    def test_reminders_cover_deadline_funding_and_expiring_requirements(self) -> None:
        archive = _archive_with_completion()
        archive.funding_nodes["n1"] = FundingNode("n1", "首期拨付", 500, T0 - timedelta(days=1))
        archive.requirements["req-fire_acceptance"] = replace(
            archive.requirements["req-fire_acceptance"], valid_until=T0 + timedelta(days=85)
        )
        archive.requirements["req-food_safety"] = replace(
            archive.requirements["req-food_safety"], valid_until=T0 - timedelta(days=1)
        )
        reminders = due_reminders(archive, T0 + timedelta(days=80))
        kinds = {(item.kind, item.ref_id) for item in reminders}
        self.assertIn(("commissioning_due", "p1"), kinds)
        self.assertIn(("funding_overdue", "n1"), kinds)
        self.assertIn(("requirement_expiring", "req-fire_acceptance"), kinds)
        self.assertIn(("requirement_expired", "req-food_safety"), kinds)
        ordered = [(item.due_at, item.kind, item.ref_id) for item in reminders]
        self.assertEqual(sorted(ordered), ordered)

    def test_waived_deadline_raises_no_commissioning_reminder(self) -> None:
        archive = _archive_with_completion()
        archive.adjustments.append(
            DeadlineAdjustment(AdjustmentKind.WAIVER, 0, "暂缓投运", "ev-waiver", "市级专员", T0)
        )
        reminders = due_reminders(archive, T0 + timedelta(days=200))
        self.assertNotIn("commissioning_due", {item.kind for item in reminders})


if __name__ == "__main__":
    unittest.main()
