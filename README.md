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


## 耗材去向与实物隔离

操作 material-arrival 按单一已知批次登记原箱数量与来源单；同箱多批次用 material-split-arrival 按供应箱原件号拆成实物段，无书证的段留空批次。material-pick 将数量拨到登记的窑车和区，material-pick-line 只扫箱号和数量登记采录行，再用 material-direct-line 指到车区；material-return 将实际退回量归还原箱。退回余量只能整体进入一次后续领用（material-pick 的 `reissue`），再领行不得再退、不得拆分。material-locate 列出一个批次的剩余库存和当前各位置保有的数量。

同箱候选数量在库存、采录行、车区和隔离位之间按箱守恒，同一实物不会在两个车区各算一份。供应追溯分两级补证：material-composition 登记箱内批次多集（只有问题批次总数时同样适用），material-packing 把可定位的装箱记录绑到原收据行。隔离口径按所有仍可行的实物分配计算：

| 裁决 | 含义 |
| --- | --- |
| `ISOLATED` | 每一可行分配中问题批次都在该 holder，物件与车区必须隔离 |
| `SUSPECT` | 存在问题批次落在该 holder 的可行分配，按最小安全范围停领 |
| `CLEAR` | 已证不可能含该批次，可继续领用 |

`isolation <lot>` 只读查看隔离指令（含各 holder 裁决、守恒账和当前冻结）。isolation-impose 把指令落成停领：受冻结的车区、整箱或隔离位拒绝新领用、再分发和退回；isolation-reconcile 仅放行已证 CLEAR 的 holder，原地停用（isolation-hold）与隔离组等操作性冻结保留到各自解除步骤。无法当场辨认的实物用 isolation-hold 原地停用，车区冻结到独立装箱证据补齐，再用 isolation-release-hold 解除。

整组可能受影响的实物经 quarantine-position 登记空置隔离位、quarantine-reserve 按仍可能一起到达的实际垫片组预留容量（容量不足拒收）、quarantine-transfer 整体转出；原车区停领只有在 quarantine-verify 凭原收据核定后才解除。晚到的原件只能解除它直接证明的实物，余量的局部正常不能覆盖另一份待核关系。同源书证原样重传幂等，冲突内容或超出原收据数量的补投拒收。
