from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import ALL_KINDS, T0, make_signatures

from childcare_commissioning.clock import FixedClock
from childcare_commissioning.domain import ClassType
from childcare_commissioning.ledger import ConcurrencyConflict, ContractViolation, Ledger


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "ledger.jsonl"
        self.clock = FixedClock(T0)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _open(self) -> Ledger:
        return Ledger(self.path, self.clock)

    def _register(self, ledger: Ledger) -> None:
        ledger.register_project(
            "p1",
            "示例市",
            100,
            rooms=[{"room_id": "r1", "name": "一楼活动室", "purpose": "保育"}],
            funding_nodes=[
                {"node_id": "n1", "name": "首期拨付", "amount": 500, "planned_date": (T0 - timedelta(days=1)).isoformat()}
            ],
            requirements=[{"requirement_id": f"req-{kind.value}", "kind": kind.value} for kind in ALL_KINDS],
        )

    def _events_on_disk(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_optimistic_concurrency_on_append(self) -> None:
        ledger = self._open()
        self._register(ledger)
        with self.assertRaises(ConcurrencyConflict) as caught:
            ledger.append(
                "MILESTONE_ACCEPTED",
                "construction_project",
                "p1",
                {"name": "completion", "accepted_by": "验收员甲", "evidence_ref": "ev-1"},
                expected_version=5,
                summary="竣工验收",
            )
        self.assertEqual(1, caught.exception.actual)
        self.assertEqual(5, caught.exception.expected)

    def test_event_violating_contract_is_rejected(self) -> None:
        ledger = self._open()
        with self.assertRaises(ContractViolation):
            ledger.append("NOT_A_TYPE", "construction_project", "p1", {}, 0, "非法事件")
        self.assertFalse(self.path.exists())

    def test_duplicate_material_is_received_once(self) -> None:
        ledger = self._open()
        self._register(ledger)
        first = ledger.receive_material("p1", "req-fire_acceptance", "m1", "hash-1")
        again = ledger.receive_material("p1", "req-fire_acceptance", "m1", "hash-1")
        same_content = ledger.receive_material("p1", "req-fire_acceptance", "m2", "hash-1")
        self.assertEqual(first["receipt_no"], again["receipt_no"])
        self.assertEqual(first["receipt_no"], same_content["receipt_no"])
        stored = [e for e in self._events_on_disk() if e["event_type"] == "MATERIAL_RECEIVED"]
        self.assertEqual(1, len(stored))

    def test_restart_resumes_pending_reviews_and_reminders(self) -> None:
        ledger = self._open()
        self._register(ledger)
        ledger.receive_material("p1", "req-food_safety", "m1", "hash-1")
        ledger.verify_requirement(
            "p1", "req-fire_acceptance", "ev-fire", "核验员丁", valid_until=T0 + timedelta(days=10)
        )
        reopened = self._open()
        pending = reopened.pending_reviews("p1")
        self.assertEqual(["m1"], [item["material_id"] for item in pending])
        reminder_kinds = {(item.kind, item.ref_id) for item in reopened.due_reminders("p1")}
        self.assertIn(("requirement_expiring", "req-fire_acceptance"), reminder_kinds)
        self.assertIn(("funding_overdue", "n1"), reminder_kinds)
        verified = reopened.archive("p1").requirements["req-fire_acceptance"]
        self.assertEqual("ev-fire", verified.evidence_ref)

    def _ready_ledger(self) -> Ledger:
        ledger = self._open()
        self._register(ledger)
        for kind in ALL_KINDS:
            ledger.verify_requirement("p1", f"req-{kind.value}", f"ev-{kind.value}", "核验员丁")
        ledger.declare_capacity("p1", "u1", ClassType.FULL_DAY, ("r1",), 20, ALL_KINDS)
        return ledger

    def test_release_requires_current_baseline(self) -> None:
        ledger = self._ready_ledger()
        baseline = ledger.baseline_version("p1", "u1")
        release = ledger.grant_release("p1", "u1", make_signatures(self.clock.now()), baseline)
        self.assertEqual(baseline, release.baseline_version)
        with self.assertRaises(ConcurrencyConflict):
            ledger.grant_release("p1", "u1", make_signatures(self.clock.now()), baseline)

    def test_remediation_invalidates_inflight_baseline(self) -> None:
        ledger = self._ready_ledger()
        baseline = ledger.baseline_version("p1", "u1")
        ledger.open_remediation("p1", "rm1", "r1", "ev-remediation")
        with self.assertRaises(ConcurrencyConflict):
            ledger.grant_release("p1", "u1", make_signatures(self.clock.now()), baseline)

    def test_release_survives_restart(self) -> None:
        ledger = self._ready_ledger()
        baseline = ledger.baseline_version("p1", "u1")
        ledger.grant_release("p1", "u1", make_signatures(self.clock.now()), baseline)
        reopened = self._open()
        release = reopened.archive("p1").releases["u1"]
        self.assertEqual(baseline, release.baseline_version)
        self.assertEqual(3, len(release.signatures))


if __name__ == "__main__":
    unittest.main()
