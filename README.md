# 托育中心投运协同台

本项目提供托育中心投运协同台的领域事件交换约定与投运协同核心库。各接入方使用统一的聚合标识、事件版本和发生时间表达业务事实；领域服务在契约之上实现从立项资金、建设里程碑、整改事项、设施房间、班型容量、人员资质、运营规章、医疗合作、试运行到正式开放的连续档案。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：中文联调样例。
- `src/childcare_commissioning/contracts.py`：不依赖第三方包的基础校验器。
- `src/childcare_commissioning/clock.py`：可注入时钟，资金节点与投运期限的计算不依赖系统时间。
- `src/childcare_commissioning/domain.py`：班型、职责、依赖项、房间、整改、资金节点等领域对象。
- `src/childcare_commissioning/release.py`：放行评估与局部冻结。
- `src/childcare_commissioning/deadlines.py`：投运期限、资金节点与到期提醒。
- `src/childcare_commissioning/ledger.py`：事件档案库，基线版本并发控制与重启恢复。
- `src/childcare_commissioning/planning.py`：需求容量规划。
- `src/childcare_commissioning/oversight.py`：城市覆盖阶段与班型证据追溯。
- `tests/`：契约边界与领域行为检查。

## 核心规则

- **放行**：服务单元（全日托、半日托、计时托）只有在其依赖项全部有效，且建设验收、运营批准、安全监督三类职责由不同人签署后才可放行。
- **局部冻结**：房间整改只冻结关联班型；中心运营状态由班型状态推导，不允许直接填报整中心已运营。
- **期限**：中央资金节点与竣工后投运期限按可注入时钟计算，延期与豁免均保留依据。
- **并发**：审批按基线版本进行，评估之后依赖流有变化即冲突需重新复核；重复材料按标识或内容哈希只收一次；档案库重启后未完成的复核与到期提醒自动恢复。
- **需求**：社区和用人单位的需求只用于容量规划，登记不得携带儿童信息，也不得抢占已确认名额。
- **监督**：主管可看到每个城市距离覆盖目标的真实阶段，并从任一开放班型追到设施、人员、制度和医疗保障证据。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
