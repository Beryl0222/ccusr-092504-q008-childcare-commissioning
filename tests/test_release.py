from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from helpers import T0, make_archive, make_signatures

from childcare_commissioning.domain import Remediation, Role, Signature
from childcare_commissioning.release import (
    CenterStatus,
    ReleaseDenied,
    UnitState,
    center_status,
    evaluate_release,
    grant_release,
    unit_state,
)


class ReleaseEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = make_archive()
        self.signatures = make_signatures()
        self.unit = self.archive.units["u1"]

    def test_release_allowed_when_dependencies_effective_and_roles_distinct(self) -> None:
        decision = evaluate_release(self.unit, self.archive, self.signatures, T0)
        self.assertTrue(decision.allowed)
        self.assertEqual((), decision.reasons)
        release = grant_release(self.unit, self.archive, self.signatures, baseline_version=3, now=T0)
        self.assertEqual(3, release.baseline_version)

    def test_missing_requirement_blocks_release(self) -> None:
        del self.archive.requirements["req-fire_acceptance"]
        decision = evaluate_release(self.unit, self.archive, self.signatures, T0)
        self.assertFalse(decision.allowed)
        self.assertIn("missing_requirement:fire_acceptance", decision.reasons)

    def test_expired_requirement_blocks_release(self) -> None:
        self.archive.requirements["req-food_safety"] = replace(
            self.archive.requirements["req-food_safety"], valid_until=T0 - timedelta(days=1)
        )
        decision = evaluate_release(self.unit, self.archive, self.signatures, T0)
        self.assertIn("requirement_not_effective:food_safety", decision.reasons)

    def test_same_person_cannot_cover_two_roles(self) -> None:
        signatures = (
            Signature(Role.CONSTRUCTION_ACCEPTOR, "同一人", T0),
            Signature(Role.OPERATIONS_APPROVER, "同一人", T0),
            Signature(Role.SAFETY_SUPERVISOR, "监督员丙", T0),
        )
        decision = evaluate_release(self.unit, self.archive, signatures, T0)
        self.assertIn("signature_role_conflict", decision.reasons)

    def test_missing_role_blocks_release(self) -> None:
        decision = evaluate_release(self.unit, self.archive, self.signatures[:2], T0)
        self.assertIn("missing_signature:safety_supervisor", decision.reasons)

    def test_denied_release_raises_with_sorted_reasons(self) -> None:
        del self.archive.requirements["req-trial_run"]
        with self.assertRaises(ReleaseDenied) as caught:
            grant_release(self.unit, self.archive, self.signatures, baseline_version=1, now=T0)
        reasons = caught.exception.decision.reasons
        self.assertIn("missing_requirement:trial_run", reasons)
        self.assertEqual(tuple(sorted(reasons)), reasons)


class FreezeScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = make_archive()
        for unit_id in ("u1", "u2", "u3"):
            grant_release(self.archive.units[unit_id], self.archive, make_signatures(), 1, T0)

    def test_room_remediation_freezes_only_related_units(self) -> None:
        self.assertIs(center_status(self.archive, T0), CenterStatus.FULLY_OPERATING)
        self.archive.remediations["rm1"] = Remediation("rm1", "r1", T0, "ev-remediation")
        self.assertIs(unit_state(self.archive.units["u1"], self.archive, T0), UnitState.FROZEN)
        self.assertIs(unit_state(self.archive.units["u3"], self.archive, T0), UnitState.FROZEN)
        self.assertIs(unit_state(self.archive.units["u2"], self.archive, T0), UnitState.OPERATING)
        self.assertIs(center_status(self.archive, T0), CenterStatus.PARTIALLY_OPERATING)

    def test_closing_remediation_unfreezes_units(self) -> None:
        self.archive.remediations["rm1"] = Remediation("rm1", "r1", T0, "ev-remediation")
        closed = replace(self.archive.remediations["rm1"], closed_at=T0 + timedelta(days=2))
        self.archive.remediations["rm1"] = closed
        self.assertIs(unit_state(self.archive.units["u1"], self.archive, T0), UnitState.OPERATING)
        self.assertIs(center_status(self.archive, T0), CenterStatus.FULLY_OPERATING)

    def test_open_remediation_blocks_new_release_on_that_room(self) -> None:
        archive = make_archive()
        archive.remediations["rm1"] = Remediation("rm1", "r1", T0, "ev-remediation")
        decision = evaluate_release(archive.units["u1"], archive, make_signatures(), T0)
        self.assertIn("room_under_remediation:r1", decision.reasons)
        other = evaluate_release(archive.units["u2"], archive, make_signatures(), T0)
        self.assertTrue(other.allowed)


class CenterStatusTests(unittest.TestCase):
    def test_center_status_is_derived_from_units(self) -> None:
        archive = make_archive()
        self.assertIs(center_status(archive, T0), CenterStatus.NOT_OPERATING)
        grant_release(archive.units["u1"], archive, make_signatures(), 1, T0)
        self.assertIs(center_status(archive, T0), CenterStatus.PARTIALLY_OPERATING)

    def test_expired_dependency_freezes_released_unit(self) -> None:
        archive = make_archive()
        grant_release(archive.units["u1"], archive, make_signatures(), 1, T0)
        archive.requirements["req-medical_agreement"] = replace(
            archive.requirements["req-medical_agreement"], valid_until=T0 + timedelta(days=1)
        )
        now = T0 + timedelta(days=2)
        self.assertIs(unit_state(archive.units["u1"], archive, now), UnitState.FROZEN)
        self.assertIs(center_status(archive, now), CenterStatus.NOT_OPERATING)


if __name__ == "__main__":
    unittest.main()
