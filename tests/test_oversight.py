from __future__ import annotations

import unittest
from datetime import timedelta

from helpers import T0, make_archive, make_signatures

from childcare_commissioning.domain import Milestone, ProjectArchive, Remediation
from childcare_commissioning.oversight import CityStage, city_report, trace_unit
from childcare_commissioning.release import UnitState, grant_release


class CityStageTests(unittest.TestCase):
    def test_stage_follows_archive_facts(self) -> None:
        archive = ProjectArchive("p1", "示例市", coverage_target=100)
        self.assertIs(city_report(archive, T0).stage, CityStage.INITIATED)
        archive.milestones.append(Milestone("foundation", T0, "验收员甲", "ev-foundation"))
        self.assertIs(city_report(archive, T0).stage, CityStage.UNDER_CONSTRUCTION)
        archive.milestones.append(Milestone("completion", T0, "验收员甲", "ev-completion"))
        self.assertIs(city_report(archive, T0).stage, CityStage.PRE_OPEN)

    def test_open_stage_counts_only_operating_units(self) -> None:
        archive = make_archive()
        grant_release(archive.units["u1"], archive, make_signatures(), 1, T0)
        self.assertIs(city_report(archive, T0).stage, CityStage.PARTIALLY_OPEN)
        for unit_id in ("u2", "u3"):
            grant_release(archive.units[unit_id], archive, make_signatures(), 1, T0)
        report = city_report(archive, T0)
        self.assertIs(report.stage, CityStage.FULLY_OPEN)
        self.assertEqual(45, report.operating_capacity)
        self.assertAlmostEqual(0.45, report.coverage_ratio)

    def test_frozen_units_drop_out_of_coverage(self) -> None:
        archive = make_archive()
        for unit_id in ("u1", "u2", "u3"):
            grant_release(archive.units[unit_id], archive, make_signatures(), 1, T0)
        archive.remediations["rm1"] = Remediation("rm1", "r1", T0, "ev-remediation")
        report = city_report(archive, T0)
        self.assertIs(report.stage, CityStage.PARTIALLY_OPEN)
        self.assertEqual(15, report.operating_capacity)
        self.assertEqual(1, report.open_remediations)


class TraceTests(unittest.TestCase):
    def test_open_unit_traces_to_facility_staff_rules_and_medical_evidence(self) -> None:
        archive = make_archive()
        grant_release(archive.units["u1"], archive, make_signatures(), 7, T0)
        bundle = trace_unit(archive, "u1", T0)
        self.assertIs(bundle.state, UnitState.OPERATING)
        self.assertEqual(("一楼活动室",), bundle.rooms)
        traced_kinds = {item.kind for item in bundle.requirements}
        self.assertIn("staff_qualification", traced_kinds)
        self.assertIn("operating_rules", traced_kinds)
        self.assertIn("medical_agreement", traced_kinds)
        self.assertTrue(all(item.evidence_ref for item in bundle.requirements))
        self.assertEqual("ev-medical_agreement", bundle.medical_evidence_ref)
        self.assertEqual(7, bundle.baseline_version)
        roles = {role for role, _signer in bundle.release_signatures}
        self.assertEqual({"construction_acceptor", "operations_approver", "safety_supervisor"}, roles)

    def test_trace_marks_missing_requirement(self) -> None:
        archive = make_archive()
        del archive.requirements["req-trial_run"]
        bundle = trace_unit(archive, "u1", T0 + timedelta(days=1))
        trial = next(item for item in bundle.requirements if item.kind == "trial_run")
        self.assertEqual("missing", trial.status)
        self.assertIs(bundle.state, UnitState.NOT_RELEASED)


if __name__ == "__main__":
    unittest.main()
