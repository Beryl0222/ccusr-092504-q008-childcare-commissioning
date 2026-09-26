# 托育中心投运协同台

中央投资支持的托育综合服务中心，主体竣工不等于可交付。本项目把**立项资金 →
建设里程碑 → 整改事项 → 设施房间 → 班型容量 → 人员资质 → 运营规章 → 医疗合作
→ 试运行 → 正式开放**串成一条连续的事件档案，并在其上提供投运放行协同能力。

## 要解决的问题

- 建设方把“完工”当交付：消防验收、食品安全制度、保育班次、妇幼机构协议各有各的
  版本，全日托 / 半日托 / 计时托共享一份不可执行的容量表。
- 局部房间整改时，不能把关联班型继续算作运营，也不能把不相关班型一起冻结。
- 建设验收、运营批准、安全监督必须由**不同角色、不同人员**完成。
- 中央资金节点与竣工后投运期限需要按**可注入时钟**判定；延期、豁免、整改都要留证。
- 社区和用人单位的需求只能用于容量规划，**不能接触儿童信息、不能抢占名额**。
- 并发审批以**基线版本**提交；重复材料只收一次；重启后继续未完成的复核与到期提醒。
- 主管能看到每个城市距离覆盖目标的真实阶段，并能从任一开放班型一路追到设施、
  人员、制度和医疗保障证据。

## 目录

- `contracts/domain.schema.json`：领域事件信封与已登记的聚合/事件类型。
- `data/sample.json`：中文联调样例。
- `src/childcare_commissioning/`
  - `contracts.py`：交换层必填字段、类型、时间与版本校验（无第三方依赖）。
  - `clock.py`：`SystemClock` / `FixedClock` / `ManualClock`，资金节点与投运期限
    统一按注入时钟判定。
  - `store.py`：事件存储。聚合流单调版本、`expected_version` 乐观并发基线、
    `event_id` 幂等去重；`JsonEventStore` 以 JSONL 落盘，重启回放。
  - `platform.py`：投运协同台领域服务（放行门、局部冻结、角色分离、豁免/延期、
    需求隔离、续办提醒、城市看板、证据链）。
- `tests/test_contracts.py`：契约边界检查。
- `tests/test_platform.py`：投运协同业务规则检查。

## 核心模型

**聚合（10 类）**：`construction_project`、`facility_room`、`service_unit`、
`compliance_requirement`、`staff_member`、`operating_regulation`、
`medical_agreement`、`demand_intake`、`material_registry`、`operating_release`。

**事件（20 类）**：`PROJECT_FUNDED`、`MILESTONE_ACCEPTED`、`ROOM_REGISTERED`、
`REMEDIATION_OPENED/CLOSED`、`REQUIREMENT_VERIFIED`、`STAFF_QUALIFIED`、
`SHIFT_ASSIGNED`、`REGULATION_SIGNED`、`MEDICAL_AGREEMENT_SIGNED`、
`CAPACITY_DECLARED`、`OWNER_ACKNOWLEDGED`、`DEMAND_RECORDED`、`MATERIAL_DEDUPED`、
`TRIAL_RUN_OPENED`、`RELEASE_APPROVED`、`SERVICE_RELEASED`、`DEADLINE_EXTENDED`、
`EXEMPTION_GRANTED`、`REVIEW_RESUMED`、`REMINDER_RAISED`。

> 注意：事件信封的 `version` 是聚合流版本号（整数），制度/协议的业务版本放在
> payload 的 `doc_version`，两者不共用字段。

## 放行规则

一个班型（`service_unit`）只有在下列依赖**全部有效且职责人已签署**时才能进入
试运行；试运行观察期满且三角色审批齐备才能正式开放：

1. 中央资金节点、主体竣工里程碑均已验收；
2. 班型绑定房间无在办整改（局部整改只冻结关联班型），房间职责人已签署；
3. 班型职责人已签署容量表；
4. 消防验收最新结论为通过；
5. 容量表引用的食品安全制度版本 = 当前签署版本（防止版本漂移）；
6. 容量表引用的妇幼机构服务协议版本 = 当前签署版本；
7. 班型有持证有效且已排班的保育人员；
8. 试运行观察期已满；
9. `CONSTRUCTION_ACCEPTANCE`、`OPERATIONS_APPROVAL`、`SAFETY_SUPERVISION`
   三角色由三名不同人员分别审批。

豁免只针对**单项依赖的当前整改实例**（记录 `remediation_anchor`，可设到期时间），
整改关闭后再立案的新整改不继承豁免；豁免绝不等同于整中心开放。

## 快速示例

```python
from datetime import datetime, timezone, timedelta
from childcare_commissioning import (
    Platform, EventStore, FixedClock, FULL_DAY,
    ROLE_CONSTRUCTION, ROLE_OPERATIONS, ROLE_SAFETY,
)

clock = FixedClock(datetime(2026, 5, 1, 9, tzinfo=timezone(timedelta(hours=8))))
pf = Platform(EventStore(clock), auto_resume=False)

pf.register_project("p1", city="甲市", name="阳光托育中心", funded_amount=5_000_000,
                    funding_due="2026-04-01T00:00:00+08:00",
                    commissioning_window_days=90, owner="建设方")
pf.accept_milestone("p1", "FUNDING_IN_PLACE", actor="财政科")
pf.accept_milestone("p1", "MAIN_COMPLETION", actor="工程科")  # 投运期限从此起算 90 天
# ... 登记房间并签署、消防核验、食品安全制度签署、妇幼协议签署、人员资质与排班 ...
pf.declare_capacity("p1", FULL_DAY, capacity=30, rooms=["r1"],
                    regulation_versions={"FOOD_SAFETY": "v1"},
                    medical_version="2026.1", owner="园长")
pf.acknowledge_owner("service_unit", "p1:FULL_DAY", owner="园长")

assert pf.evaluate_gate("p1", FULL_DAY).dependencies_valid
pf.open_trial_run("p1", FULL_DAY, min_trial_days=7)
clock.advance(days=7)
pf.approve_release("p1", FULL_DAY, role=ROLE_CONSTRUCTION, actor="建设验收员")
pf.approve_release("p1", FULL_DAY, role=ROLE_OPERATIONS, actor="运营批准员")
pf.approve_release("p1", FULL_DAY, role=ROLE_SAFETY, actor="安全监督员")
pf.formally_open("p1", FULL_DAY)
```

### 并发审批采用基线版本

```python
baseline = store.version("p1:FULL_DAY:release")
pf.approve_release("p1", FULL_DAY, role=ROLE_CONSTRUCTION, actor="甲",
                   expected_version=baseline)
# 另一审批人仍用同一基线提交 → ConcurrentModificationError，须重读后再提交
```

### 重启续办与到期提醒

```python
pf = Platform(JsonEventStore("journal.jsonl", clock))   # 回放全部历史
pf.resume_pending_reviews()      # 对在办整改留一条 REVIEW_RESUMED（幂等）
pf.run_due_reviews()             # 按注入时钟扫描资金/投运/整改/豁免到期，同日不重复
```

### 主管视图

- `city_dashboard()`：城市覆盖阶段 `NOT_STARTED / BUILDING / TRIAL / OPENING /
  COVERED`，试运行容量与正式开放容量分列。
- `center_status(project_id)`：逐班型真实状态，`fully_operational` 要求所有已登记
  班型都开放且无冻结——一个班型整改中就不虚报整中心运营。
- `evidence_chain(project_id, unit)`：从班型追到容量事件、房间/整改记录、人员资质
  与排班、制度签署、消防核验、医疗协议、试运行与三方审批。
- `capacity_plan(project_id)`：需求仅输出分来源/分龄聚合席位，`contact`、
  `child_name` 不进入规划视图，且 `confirmed_reservations_from_demand` 恒为 0。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```
