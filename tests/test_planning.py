from __future__ import annotations

import unittest

from helpers import T0, make_archive, make_signatures

from childcare_commissioning.domain import ClassType
from childcare_commissioning.planning import DemandRecord, plan_capacity, validate_demand
from childcare_commissioning.release import grant_release


class DemandValidationTests(unittest.TestCase):
    def test_child_information_is_rejected(self) -> None:
        payload = {
            "demand_id": "d1",
            "source": "community",
            "class_type": "full_day",
            "headcount": 30,
            "child_name": "小明",
            "guardian_phone": "13800000000",
        }
        codes = {(issue.field, issue.code) for issue in validate_demand(payload)}
        self.assertIn(("child_name", "child_data_forbidden"), codes)
        self.assertIn(("guardian_phone", "child_data_forbidden"), codes)

    def test_anonymous_demand_passes(self) -> None:
        payload = {"demand_id": "d1", "source": "employer", "class_type": "hourly", "headcount": 12}
        self.assertEqual([], validate_demand(payload))

    def test_invalid_headcount_and_class_type_are_reported(self) -> None:
        payload = {"demand_id": "d1", "source": "community", "class_type": "weekend", "headcount": 0}
        codes = {(issue.field, issue.code) for issue in validate_demand(payload)}
        self.assertIn(("headcount", "positive_integer"), codes)
        self.assertIn(("class_type", "unsupported_value"), codes)


class CapacityPlanTests(unittest.TestCase):
    def test_confirmed_slots_are_never_reallocated_to_demand(self) -> None:
        archive = make_archive()
        grant_release(archive.units["u1"], archive, make_signatures(), 1, T0)
        archive.confirmed_enrollments["u1"] = 20
        demands = (DemandRecord("d1", "community", ClassType.FULL_DAY, 30, T0),)
        plans = {plan.class_type: plan for plan in plan_capacity(archive, demands, T0)}
        full_day = plans[ClassType.FULL_DAY]
        self.assertEqual(20, full_day.operating_capacity)
        self.assertEqual(20, full_day.confirmed)
        self.assertEqual(0, full_day.available)
        self.assertEqual(30, full_day.gap)
        self.assertEqual(20, archive.confirmed_enrollments["u1"])

    def test_unreleased_units_contribute_no_capacity(self) -> None:
        archive = make_archive()
        demands = (DemandRecord("d1", "employer", ClassType.HALF_DAY, 10, T0),)
        plans = {plan.class_type: plan for plan in plan_capacity(archive, demands, T0)}
        half_day = plans[ClassType.HALF_DAY]
        self.assertEqual(0, half_day.operating_capacity)
        self.assertEqual(10, half_day.gap)

    def test_demand_is_aggregated_per_class_type(self) -> None:
        archive = make_archive()
        for unit_id in ("u1", "u2", "u3"):
            grant_release(archive.units[unit_id], archive, make_signatures(), 1, T0)
        demands = (
            DemandRecord("d1", "community", ClassType.HOURLY, 3, T0),
            DemandRecord("d2", "employer", ClassType.HOURLY, 4, T0),
        )
        plans = {plan.class_type: plan for plan in plan_capacity(archive, demands, T0)}
        hourly = plans[ClassType.HOURLY]
        self.assertEqual(7, hourly.demand_headcount)
        self.assertEqual(10, hourly.available)
        self.assertEqual(0, hourly.gap)


if __name__ == "__main__":
    unittest.main()
