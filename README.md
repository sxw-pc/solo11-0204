# 陶瓷窑车拼烧与窑位验收采录台

供合成陶瓷代工厂离线记录跨客户共享烧制批。窑务员把坯件排到窑车温位，质检员按该位置的记录仪数据和独立见证片作出分件决定，发货员按客户分单交付和结算。夹具里的客户、色方和记录均为虚构。

## 运行

需要 Python 3.11 或更新版本，自带 SQLite，无第三方依赖。在项目根目录执行，`kiln.sqlite` 首次运行时创建。

```shell
python -B -m kiln_cars --db kiln.sqlite demo
python -B -m kiln_cars --db kiln.sqlite audit
python -B -m unittest discover -s tests -v
```

`demo` 向新数据库逐事务写入 `fixtures/shared.json`。其中标记 `refused` 的步骤应被拒并回滚。如果演示中途遇到意外失败，先前提交仍可查验，完整重演可使用新的数据库路径。`action` 每次执行一个操作，参数是 JSON 对象。以下为 PowerShell 示例。

```powershell
python -B -m kiln_cars --db kiln.sqlite action zone '{"piece_id":"P2"}'
python -B -m kiln_cars --db kiln.sqlite action balance '{"order_id":"O-A"}'
python -B -m kiln_cars --db kiln.sqlite action --help
```

## 操作与口径

| 工作 | 命令 | 记录与决定 |
| --- | --- | --- |
| 共享批排位 | `order`, `recipe`, `piece`, `firing`, `slot`, `place`, `unload`, `lock` | 每件记录客户与色方，每个车区可放一件。气氛和计划温度必须相容。一件的色方排放与同一烧制批内任何坯件的敏感项相撞即拒绝。锁批后位置冻结。 |
| 窑位证据 | `trace`, `witness`, `zone` | 把一分钟温度归到烧制批、车、区和分钟。原样重传幂等，冲突拒收。每段连续缺口需要同区、同范围的独立见证片，且所有匹配片的温度范围都须在坯体温窗内。实测超窗优先判拒，晚到的实测可能改变缺口范围。 |
| 分件验收与返工 | `assess`, `rework-slot`, `reserve`, `finish-rework` | 按表面检查、热证据、客户降级授权形成验收修订。返工先扣占兼容坯体的空位，再登记完工或释放。已判返工件须完工并重新验收才能交付。返工占位后同区证据更新，先释放、重检，再视决定扣位。 |
| 分单交付结算 | `dispatch`, `balance` | 同订单可分次交付。交付要求当前且可交付的分件验收，按合同等级用整数分计价。跨单混入、重复交件及过时验收导致整单事务回滚。已过账单号按同一订单与件集幂等。 |
| 复核 | `audit` 或 `recompute` | 只读打开现有数据库，重算位置相容性，并按留存的证据截止序号复核历史验收、返工资格和结算。已交付件收到同区新证据时，单列当前热结论，原验收与账款保留。 |

演示中 `D-A1` 合计 2050 分，返工后 `D-A2` 合计 1250 分。`P2` 已降级交付后补入超窗记录，审计保留原单行并列出 `OUTSIDE_WINDOW`。`P4` 的订单无降级授权，夹具保留其待返工状态。

数据库按操作获取 SQLite 写事务，冲突检查、扣占和过账在事务内完成。独立见证人使用与记录仪责任方不同的登记标识，登记员负责核对现场身份和见证片来源。温窗和色方规则是本地夹具口径，CLI 只记录业务证据和决定。

