from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from childcare_commissioning import (  # noqa: E402
    FULL_DAY,
    HALF_DAY,
    HOURLY,
    ROLE_CONSTRUCTION,
    ROLE_OPERATIONS,
    ROLE_SAFETY,
    ConcurrentModificationError,
    DomainError,
    EventStore,
    FixedClock,
    JsonEventStore,
    Platform,
)
from childcare_commissioning.contracts import validate_event  # noqa: E402

CST = timezone(timedelta(hours=8))


def clock_at(year: int, month: int, day: int) -> FixedClock:
    return FixedClock(datetime(year, month, day, 9, 0, tzinfo=CST))


def make_ready(
    clock: FixedClock,
    project_id: str = "p1",
    unit: str = FULL_DAY,
    *,
    city: str = "甲市",
    rooms: tuple[str, ...] = ("r1",),
    trial_days: int = 0,
) -> Platform:
    """登记一个放行门全部满足、但尚未试运行的项目。"""
    store = EventStore(clock)
    pf = Platform(store, auto_resume=False)
    pf.register_project(
        project_id,
        city=city,
        name="阳光托育中心",
        funded_amount=5_000_000,
        funding_due="2026-04-01T00:00:00+08:00",
        commissioning_window_days=90,
        owner="建设方",
    )
    pf.accept_milestone(project_id, "FUNDING_IN_PLACE", actor="财政科")
    pf.accept_milestone(project_id, "MAIN_COMPLETION", actor="工程科")
    for room_id in rooms:
        pf.register_room(project_id, room_id, name=f"房间{room_id}", owner="房管员")
        pf.acknowledge_owner("facility_room", room_id, owner="房管员")
    pf.verify_compliance(project_id, "fire-1", kind="FIRE_ACCEPTANCE", valid=True, actor="消防科")
    pf.sign_regulation(project_id, "reg-food", kind="FOOD_SAFETY", version="v1", owner="食安员")
    pf.sign_medical_agreement(
        project_id, "med-1", partner="市妇幼保健院", version="2026.1", owner="医联员"
    )
    pf.register_staff(
        project_id, "s1", name="陈保育", qualification="保育员证", qualification_no="BY001",
        owner="人事",
    )
    pf.assign_shift(project_id, "s1", unit, scheduler="排班员")
    pf.declare_capacity(
        project_id,
        unit,
        capacity=30,
        rooms=list(rooms),
        regulation_versions={"FOOD_SAFETY": "v1"},
        medical_version="2026.1",
        owner="园长",
    )
    pf.acknowledge_owner("service_unit", f"{project_id}:{unit}", owner="园长")
    return pf


def approve_all_and_open(pf: Platform, project_id: str, unit: str) -> None:
    pf.approve_release(project_id, unit, role=ROLE_CONSTRUCTION, actor="建设验收员")
    pf.approve_release(project_id, unit, role=ROLE_OPERATIONS, actor="运营批准员")
    pf.approve_release(project_id, unit, role=ROLE_SAFETY, actor="安全监督员")
    pf.formally_open(project_id, unit)


class GateTests(unittest.TestCase):
    def test_happy_path_to_formal_open(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock, trial_days=7)
        self.assertTrue(pf.evaluate_gate("p1", FULL_DAY).dependencies_valid)

        pf.open_trial_run("p1", FULL_DAY, min_trial_days=7)
        with self.assertRaises(DomainError):
            pf.formally_open("p1", FULL_DAY)  # 观察期未满
        clock.advance(days=7)
        approve_all_and_open(pf, "p1", FULL_DAY)
        self.assertEqual("OPEN", pf.unit_progress("p1", FULL_DAY).status)

    def test_completion_is_not_delivery(self) -> None:
        """建设方把“完工”当交付：只验收竣工时班型仍不可放行。"""
        clock = clock_at(2026, 5, 1)
        store = EventStore(clock)
        pf = Platform(store, auto_resume=False)
        pf.register_project(
            "p1", city="甲市", name="中心", funded_amount=100,
            funding_due="2026-04-01T00:00:00+08:00",
            commissioning_window_days=90, owner="建设方",
        )
        pf.accept_milestone("p1", "FUNDING_IN_PLACE", actor="财政")
        pf.accept_milestone("p1", "MAIN_COMPLETION", actor="工程")
        codes = {b.code for b in pf.evaluate_gate("p1", FULL_DAY).blockers}
        self.assertIn("CAPACITY_NOT_DECLARED", codes)
        self.assertFalse(pf.center_status("p1")["fully_operational"])

    def test_unsigned_owner_blocks_after_capacity_declared(self) -> None:
        clock = clock_at(2026, 5, 1)
        store = EventStore(clock)
        pf = Platform(store, auto_resume=False)
        pf.register_project(
            "p1", city="甲市", name="中心", funded_amount=100,
            funding_due="2026-04-01T00:00:00+08:00",
            commissioning_window_days=90, owner="建设方",
        )
        pf.accept_milestone("p1", "FUNDING_IN_PLACE", actor="财政")
        pf.accept_milestone("p1", "MAIN_COMPLETION", actor="工程")
        pf.register_room("p1", "r1", name="室", owner="房管员")  # 刻意不签署
        pf.verify_compliance("p1", "fire", kind="FIRE_ACCEPTANCE", valid=True, actor="消防")
        pf.sign_regulation("p1", "reg", kind="FOOD_SAFETY", version="v1", owner="食")
        pf.sign_medical_agreement("p1", "med", partner="妇幼", version="v1", owner="医")
        pf.register_staff("p1", "s", name="保", qualification="证", qualification_no="1", owner="人")
        pf.assign_shift("p1", "s", FULL_DAY, scheduler="排")
        pf.declare_capacity(
            "p1", FULL_DAY, capacity=10, rooms=["r1"],
            regulation_versions={"FOOD_SAFETY": "v1"}, medical_version="v1", owner="园",
        )
        pf.acknowledge_owner("service_unit", "p1:FULL_DAY", owner="园")
        codes = {b.code for b in pf.evaluate_gate("p1", FULL_DAY).blockers}
        self.assertIn("ROOM_OWNER_UNSIGNED", codes)
        with self.assertRaises(DomainError):
            pf.open_trial_run("p1", FULL_DAY)

    def test_unit_owner_signature_required(self) -> None:
        clock = clock_at(2026, 5, 1)
        store = EventStore(clock)
        pf = Platform(store, auto_resume=False)
        pf.register_project(
            "p1", city="甲市", name="中心", funded_amount=100,
            funding_due="2026-04-01T00:00:00+08:00",
            commissioning_window_days=90, owner="建设方",
        )
        pf.accept_milestone("p1", "FUNDING_IN_PLACE", actor="财政")
        pf.accept_milestone("p1", "MAIN_COMPLETION", actor="工程")
        pf.register_room("p1", "r1", name="室", owner="房管员")
        pf.acknowledge_owner("facility_room", "r1", owner="房管员")
        pf.verify_compliance("p1", "fire", kind="FIRE_ACCEPTANCE", valid=True, actor="消防")
        pf.sign_regulation("p1", "reg", kind="FOOD_SAFETY", version="v1", owner="食")
        pf.sign_medical_agreement("p1", "med", partner="妇幼", version="v1", owner="医")
        pf.register_staff("p1", "s", name="保", qualification="证", qualification_no="1", owner="人")
        pf.assign_shift("p1", "s", FULL_DAY, scheduler="排")
        pf.declare_capacity(
            "p1", FULL_DAY, capacity=10, rooms=["r1"],
            regulation_versions={"FOOD_SAFETY": "v1"}, medical_version="v1", owner="园",
        )  # 班型职责人未签署
        codes = {b.code for b in pf.evaluate_gate("p1", FULL_DAY).blockers}
        self.assertIn("UNIT_OWNER_UNSIGNED", codes)

    def test_fire_food_medical_staffing_each_required(self) -> None:
        clock = clock_at(2026, 5, 1)
        pf = make_ready(clock)
        # 消防复验不通过
        pf.verify_compliance("p1", "fire-2", kind="FIRE_ACCEPTANCE", valid=False, actor="消防科")
        codes = {b.code for b in pf.evaluate_gate("p1", FULL_DAY).blockers}
        self.assertIn("FIRE_ACCEPTANCE_MISSING", codes)

        # 食品安全制度出新版，容量表引用版本漂移
        pf.verify_compliance("p1", "fire-3", kind="FIRE_ACCEPTANCE", valid=True, actor="消防科")
        pf.sign_regulation(
            "p1", "reg-food-v2", kind="FOOD_SAFETY", version="v2", owner="食安员"
        )
        codes = {b.code for b in pf.evaluate_gate("p1", FULL_DAY).blockers}
        self.assertIn("REGULATION_VERSION_DRIFT", codes)

        # 医疗协议出新版
        pf.sign_regulation("p1", "reg-food", kind="FOOD_SAFETY", version="v1", owner="食安员")
        pf.sign_medical_agreement(
            "p1", "med-2", partner="市妇幼保健院", version="2026.2", owner="医联员"
        )
        codes = {b.code for b in pf.evaluate_gate("p1", FULL_DAY).blockers}
        self.assertIn("MEDICAL_VERSION_DRIFT", codes)

    def test_revoked_qualification_blocks_open_unit(self) -> None:
        clock = clock_at(2026, 5, 1)
        pf = make_ready(clock)
        self.assertTrue(pf.evaluate_gate("p1", FULL_DAY).dependencies_valid)
        # 保育人员资质复核失效（如证件被吊销），班型立即失去人员保障
        pf.revalidate_staff_qualification(
            "s1", valid=False, actor="人社复核", reason="证件到期未续"
        )
        codes = {b.code for b in pf.evaluate_gate("p1", FULL_DAY).blockers}
        self.assertIn("STAFFING_MISSING", codes)
        with self.assertRaises(DomainError):
            pf.open_trial_run("p1", FULL_DAY)
        # 复核恢复后放行门通过
        pf.revalidate_staff_qualification(
            "s1", valid=True, actor="人社复核", reason="新证核发"
        )
        self.assertTrue(pf.evaluate_gate("p1", FULL_DAY).dependencies_valid)


class RoleSeparationTests(unittest.TestCase):
    def test_three_roles_must_be_different_people(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock)
        pf.open_trial_run("p1", FULL_DAY)
        pf.approve_release("p1", FULL_DAY, role=ROLE_CONSTRUCTION, actor="甲")
        with self.assertRaises(DomainError):
            pf.approve_release("p1", FULL_DAY, role=ROLE_SAFETY, actor="甲")
        pf.approve_release("p1", FULL_DAY, role=ROLE_OPERATIONS, actor="乙")
        with self.assertRaises(DomainError):
            pf.formally_open("p1", FULL_DAY)  # 缺安全监督
        pf.approve_release("p1", FULL_DAY, role=ROLE_SAFETY, actor="丙")
        pf.formally_open("p1", FULL_DAY)

    def test_role_cannot_approve_twice(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock)
        pf.open_trial_run("p1", FULL_DAY)
        pf.approve_release("p1", FULL_DAY, role=ROLE_CONSTRUCTION, actor="甲")
        with self.assertRaises(DomainError):
            pf.approve_release("p1", FULL_DAY, role=ROLE_CONSTRUCTION, actor="丁")


class LocalFreezeTests(unittest.TestCase):
    def test_room_remediation_freezes_only_linked_unit(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock, unit=FULL_DAY, rooms=("r1",))
        # 半日托使用另一房间 r2
        pf.register_room("p1", "r2", name="房间r2", owner="房管员")
        pf.acknowledge_owner("facility_room", "r2", owner="房管员")
        pf.assign_shift("p1", "s1", HALF_DAY, scheduler="排班员")
        pf.declare_capacity(
            "p1", HALF_DAY, capacity=15, rooms=["r2"],
            regulation_versions={"FOOD_SAFETY": "v1"},
            medical_version="2026.1", owner="园长",
        )
        pf.acknowledge_owner("service_unit", "p1:HALF_DAY", owner="园长")

        # 全日托、半日托均开放，整中心才真实运营
        pf.open_trial_run("p1", FULL_DAY)
        approve_all_and_open(pf, "p1", FULL_DAY)
        pf.open_trial_run("p1", HALF_DAY)
        approve_all_and_open(pf, "p1", HALF_DAY)
        self.assertTrue(pf.center_status("p1")["fully_operational"])

        # r1 整改只冻结全日托，半日托不受影响，整中心不得虚报运营
        pf.open_remediation(
            "r1", issue="地板防滑整改", owner="维修班",
            due="2026-05-20T00:00:00+08:00",
        )
        full = pf.evaluate_gate("p1", FULL_DAY)
        half = pf.evaluate_gate("p1", HALF_DAY)
        self.assertTrue(full.frozen)
        self.assertFalse(half.frozen)
        self.assertEqual("FROZEN", pf.unit_progress("p1", FULL_DAY).status)
        self.assertEqual("OPEN", pf.unit_progress("p1", HALF_DAY).status)
        self.assertFalse(pf.center_status("p1")["fully_operational"])

        # 关闭整改后恢复
        pf.close_remediation("r1", actor="维修班")
        self.assertTrue(pf.evaluate_gate("p1", FULL_DAY).dependencies_valid)
        self.assertEqual("OPEN", pf.unit_progress("p1", FULL_DAY).status)

    def test_exemption_is_bound_to_remediation_instance(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock)
        pf.open_remediation(
            "r1", issue="外观瑕疵", owner="维修班",
            due="2026-05-20T00:00:00+08:00",
        )
        self.assertTrue(pf.evaluate_gate("p1", FULL_DAY).frozen)
        pf.grant_exemption(target_aggregate="r1", reason="不影响安全", approver="监督组")
        self.assertFalse(pf.evaluate_gate("p1", FULL_DAY).frozen)

        # 整改关闭后再立案新整改，旧豁免不得继承
        pf.close_remediation("r1", actor="维修班")
        pf.open_remediation(
            "r1", issue="新发现隐患", owner="维修班",
            due="2026-05-25T00:00:00+08:00",
        )
        self.assertTrue(pf.evaluate_gate("p1", FULL_DAY).frozen)

    def test_expired_exemption_restores_freeze(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock)
        pf.open_remediation(
            "r1", issue="待复验项", owner="维修班",
            due="2026-06-10T00:00:00+08:00",
        )
        pf.grant_exemption(
            target_aggregate="r1", reason="限时豁免", approver="监督组",
            expires_at="2026-05-15T00:00:00+08:00",
        )
        self.assertFalse(pf.evaluate_gate("p1", FULL_DAY).frozen)
        clock.advance(days=6)
        self.assertTrue(pf.evaluate_gate("p1", FULL_DAY).frozen)

    def test_exemption_requires_active_remediation(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock)
        with self.assertRaises(DomainError):
            pf.grant_exemption(target_aggregate="r1", reason="无整改可豁免", approver="监督组")


class ClockAndDeadlineTests(unittest.TestCase):
    def test_commissioning_window_starts_at_completion(self) -> None:
        clock = clock_at(2026, 5, 1)
        pf = make_ready(clock)
        # 竣工时间 2026-05-01 + 90 天
        self.assertTrue(pf.projects["p1"]["commissioning_due"].startswith("2026-07-30"))

    def test_extension_requires_later_date_and_keeps_basis(self) -> None:
        clock = clock_at(2026, 5, 1)
        pf = make_ready(clock)
        with self.assertRaises(DomainError):
            pf.extend_commissioning_deadline(
                "p1", new_deadline="2026-07-01T00:00:00+08:00", reason="提前", actor="x"
            )
        pf.extend_commissioning_deadline(
            "p1", new_deadline="2026-09-30T00:00:00+08:00",
            reason="雨季影响室外工程", actor="发改委", material_id="M-WX",
        )
        self.assertTrue(pf.projects["p1"]["commissioning_due"].startswith("2026-09-30"))
        ext = [e for e in pf.store.events("p1") if e.event_type == "DEADLINE_EXTENDED"]
        self.assertEqual("M-WX", ext[-1].payload["material_id"])

    def test_overdue_reminders_follow_injected_clock(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock)
        pf.open_remediation(
            "r1", issue="逾期整改", owner="维修班",
            due="2026-05-05T00:00:00+08:00",
        )
        reminders = pf.run_due_reviews()
        self.assertTrue(any("房间整改已逾期" in e.summary for e in reminders))
        # 同一天重复扫描不重复提醒
        self.assertEqual([], pf.run_due_reviews())
        # 次日继续提醒
        clock.advance(days=1)
        self.assertEqual(1, len(pf.run_due_reviews()))

    def test_funding_node_overdue_reminder(self) -> None:
        clock = clock_at(2026, 5, 10)
        store = EventStore(clock)
        pf = Platform(store, auto_resume=False)
        pf.register_project(
            "p1", city="甲市", name="中心", funded_amount=100,
            funding_due="2026-05-01T00:00:00+08:00",
            commissioning_window_days=30, owner="建设方",
        )
        reminders = pf.run_due_reviews()
        self.assertTrue(any("中央资金节点已逾期" in e.summary for e in reminders))


class DemandPrivacyTests(unittest.TestCase):
    def test_demand_feeds_planning_without_child_data_or_reservation(self) -> None:
        clock = clock_at(2026, 5, 1)
        pf = make_ready(clock)
        pf.record_community_demand(
            "p1", source="COMMUNITY", seats=10, age_band="2-3岁"
        )
        pf.record_community_demand(
            "p1", source="EMPLOYER", seats=6, age_band="1-2岁"
        )
        # 需求通道在入口即拒绝任何儿童信息或联系方式
        with self.assertRaises(DomainError):
            pf.record_community_demand(
                "p1", source="COMMUNITY", seats=1, age_band="2-3岁",
                child_name="小明",
            )
        with self.assertRaises(DomainError):
            pf.record_community_demand(
                "p1", source="EMPLOYER", seats=1, age_band="2-3岁",
                contact="李经理 13800000000",
            )
        plan = pf.capacity_plan("p1")
        serialized = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn("小明", serialized)
        self.assertNotIn("13800000000", serialized)
        self.assertNotIn("李经理", serialized)
        self.assertEqual(0, plan["confirmed_reservations_from_demand"])
        buckets = {(b["source"], b["age_band"]): b["seats"] for b in plan["demand_buckets"]}
        self.assertEqual(10, buckets[("COMMUNITY", "2-3岁")])
        self.assertEqual(6, buckets[("EMPLOYER", "1-2岁")])

    def test_demand_source_must_be_registered_kind(self) -> None:
        clock = clock_at(2026, 5, 1)
        pf = make_ready(clock)
        with self.assertRaises(DomainError):
            pf.record_community_demand(
                "p1", source="PERSONAL", seats=1, age_band="2-3岁"
            )


class ConcurrencyAndMaterialTests(unittest.TestCase):
    def test_stale_baseline_rejected(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock)
        pf.open_trial_run("p1", FULL_DAY)
        release_id = "p1:FULL_DAY:release"
        baseline = pf.store.version(release_id)
        pf.approve_release(
            "p1", FULL_DAY, role=ROLE_CONSTRUCTION, actor="甲",
            expected_version=baseline,
        )
        with self.assertRaises(ConcurrentModificationError):
            pf.approve_release(
                "p1", FULL_DAY, role=ROLE_OPERATIONS, actor="乙",
                expected_version=baseline,
            )
        # 重读基线后提交成功
        pf.approve_release(
            "p1", FULL_DAY, role=ROLE_OPERATIONS, actor="乙",
            expected_version=pf.store.version(release_id),
        )

    def test_duplicate_material_collected_once(self) -> None:
        clock = clock_at(2026, 5, 1)
        pf = make_ready(clock)
        first = pf.submit_material(
            material_id="M-1", kind="消防意见书", fingerprint="sha256:abc", submitted_by="甲"
        )
        second = pf.submit_material(
            material_id="M-2", kind="消防意见书", fingerprint="sha256:abc", submitted_by="乙"
        )
        self.assertFalse(first.payload["duplicate"])
        self.assertTrue(second.payload["duplicate"])
        self.assertEqual("M-1", second.payload["original_material_id"])


class RestartTests(unittest.TestCase):
    def test_replay_resumes_review_and_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            clock = clock_at(2026, 5, 1)
            store = JsonEventStore(path, clock)
            pf = Platform(store, auto_resume=False)
            pf.register_project(
                "p1", city="甲市", name="中心", funded_amount=100,
                funding_due="2026-06-01T00:00:00+08:00",
                commissioning_window_days=30, owner="建设方",
            )
            pf.register_room("p1", "r1", name="室", owner="管")
            pf.open_remediation(
                "r1", issue="未完成整改", owner="维修",
                due="2026-05-05T00:00:00+08:00",
            )

            # 重启：读模型完整回放，且对未完成整改续记一次 REVIEW_RESUMED
            pf2 = Platform(JsonEventStore(path, clock))
            resumes = [e for e in pf2.store.events() if e.event_type == "REVIEW_RESUMED"]
            self.assertEqual(1, len(resumes))
            self.assertEqual("r1", resumes[0].aggregate_id)

            # 再次重启不重复续记
            pf3 = Platform(JsonEventStore(path, clock))
            self.assertEqual(
                1, sum(1 for e in pf3.store.events() if e.event_type == "REVIEW_RESUMED")
            )

            # 到期提醒继续工作
            clock.advance(days=10)
            due = pf3.run_due_reviews()
            self.assertTrue(any("房间整改已逾期" in e.summary for e in due))
            self.assertEqual([], pf3.run_due_reviews())


class DashboardAndEvidenceTests(unittest.TestCase):
    def test_city_stage_reflects_reality(self) -> None:
        clock = clock_at(2026, 5, 10)
        # 甲市：已开放 30 席，目标 50 → OPENING
        pf = make_ready(clock, project_id="p1", city="甲市")
        pf.open_trial_run("p1", FULL_DAY)
        approve_all_and_open(pf, "p1", FULL_DAY)
        # 乙市：仅有项目在建 → BUILDING
        pf.register_project(
            "p2", city="乙市", name="乙中心", funded_amount=100,
            funding_due="2026-06-01T00:00:00+08:00",
            commissioning_window_days=30, owner="建设方",
        )
        pf.set_coverage_target("甲市", 50)
        pf.set_coverage_target("丙市", 20)
        rows = {r.city: r for r in pf.city_dashboard()}
        self.assertEqual("OPENING", rows["甲市"].stage)
        self.assertEqual(30, rows["甲市"].open_capacity)
        self.assertEqual("BUILDING", rows["乙市"].stage)
        self.assertEqual("NOT_STARTED", rows["丙市"].stage)

        # 甲市目标下调到 30 → COVERED
        pf.set_coverage_target("甲市", 30)
        self.assertEqual("COVERED", {r.city: r for r in pf.city_dashboard()}["甲市"].stage)

    def test_trial_capacity_counts_separately(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock)
        pf.open_trial_run("p1", FULL_DAY)
        rows = {r.city: r for r in pf.city_dashboard()}
        self.assertEqual("TRIAL", rows["甲市"].stage)
        self.assertEqual(30, rows["甲市"].trial_capacity)
        self.assertEqual(0, rows["甲市"].open_capacity)

    def test_evidence_chain_from_open_unit(self) -> None:
        clock = clock_at(2026, 5, 10)
        pf = make_ready(clock)
        pf.open_trial_run("p1", FULL_DAY)
        approve_all_and_open(pf, "p1", FULL_DAY)
        chain = pf.evidence_chain("p1", FULL_DAY)
        self.assertEqual("capacity:p1:FULL_DAY", chain["capacity_event"])
        self.assertEqual("release:p1:FULL_DAY", chain["released_event"])
        self.assertEqual(["r1"], [room["room_id"] for room in chain["rooms"]])
        self.assertEqual(1, len(chain["staff"]))
        self.assertEqual("s1", chain["staff"][0]["staff_id"])
        self.assertTrue(chain["staff"][0]["valid"])
        self.assertTrue(any(r["kind"] == "FOOD_SAFETY" for r in chain["regulations"]))
        self.assertTrue(chain["medical_agreements"])
        self.assertTrue(any(c["kind"] == "FIRE_ACCEPTANCE" for c in chain["compliance"]))
        roles = {a["role"] for a in chain["release_approvals"]}
        self.assertEqual(
            {ROLE_CONSTRUCTION, ROLE_OPERATIONS, ROLE_SAFETY}, roles
        )
        actors = [a["actor"] for a in chain["release_approvals"]]
        self.assertEqual(len(actors), len(set(actors)))  # 三角色不同人


class ContractConsistencyTests(unittest.TestCase):
    def test_every_registered_event_type_is_valid_contract(self) -> None:
        schema = json.loads(
            (ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8")
        )
        registered = set(schema["properties"]["event_type"]["enum"])

        with tempfile.TemporaryDirectory() as tmp:
            clock = clock_at(2026, 5, 1)
            pf = make_ready(clock)
            journal = Path(tmp) / "journal.jsonl"
            with journal.open("w", encoding="utf-8") as handle:
                for event in pf.store.events():
                    handle.write(json.dumps(dict(event.payload), ensure_ascii=False))
                    handle.write("\n")
            del pf

            replayed = Platform(JsonEventStore(journal, clock))
            # 在办整改 + 重启 → REVIEW_RESUMED
            replayed.open_remediation(
                "r1", issue="复验项", owner="维修",
                due="2026-05-20T00:00:00+08:00",
            )
            replayed2 = Platform(JsonEventStore(journal, clock))
            replayed2.grant_exemption(
                target_aggregate="r1", reason="不影响安全", approver="监督组"
            )
            replayed2.close_remediation("r1", actor="维修")
            replayed2.submit_material(
                material_id="M-1", kind="纪要", fingerprint="h1", submitted_by="甲"
            )
            replayed2.submit_material(
                material_id="M-2", kind="纪要", fingerprint="h1", submitted_by="乙"
            )
            replayed2.record_community_demand(
                "p1", source="COMMUNITY", seats=8, age_band="2-3岁"
            )
            replayed2.extend_commissioning_deadline(
                "p1", new_deadline="2026-09-30T00:00:00+08:00",
                reason="雨季工程延期", actor="发改委",
            )
            replayed2.open_trial_run("p1", FULL_DAY, min_trial_days=7)
            clock.advance(days=7)
            approve_all_and_open(replayed2, "p1", FULL_DAY)
            replayed2.open_remediation(
                "r1", issue="开放后逾期复查项", owner="维修",
                due="2026-05-05T00:00:00+08:00",
            )
            replayed2.run_due_reviews()

            seen = set()
            for event in replayed2.store.events():
                issues = validate_event(dict(event.payload), schema)
                self.assertEqual(
                    [], [(i.field, i.code) for i in issues], msg=event.event_type
                )
                seen.add(event.event_type)
            self.assertEqual(registered, seen)


if __name__ == "__main__":
    unittest.main()
