# 事件契约注册服务（Event Contract Registry）

带版本的 schema 注册表 + 兼容性推演引擎 + 发布判定，回答三个问题：

1. **重命名字段**：谁读不了？双写能不能救？
2. **填默认值 / 可选转必填**：生产者省略字段时，消费者会拿到什么？消费者自己的默认值不同会怎样？
3. **一批历史消息重放**：哪一个（生产者版本, 消费者）对、哪一个字段先崩？

零依赖：Python 标准库 + 原生 JS/CSS。

## 运行

```bash
python3 -I -c "
import sys; sys.path.insert(0,'/workspace')
from event_contract_registry.server import main
raise SystemExit(main(['--port','8765','--db','/workspace/var/registry.db','--fresh']))
"
```

打开 <http://127.0.0.1:8765>。`--no-seed` 不灌演示数据；`--fresh` 先删库。

## 测试

```bash
python3 -I -m unittest discover -s tests        # 38 项：引擎 + HTTP API
node tests/render_check.js                      # 真 app.js 在 DOM shim 上跑一遍（需服务已启动）
```

## 判定是怎么来的

每个消费者登记三件事：钉住的 `contract_version`、`mode`（`backlog` 会重放历史 / `new_only` 只收新消息）、以及可选的每字段 `overrides`（自带的默认值、放宽的类型、额外接受的旧名）。

发布候选版本 vN 时，对每个消费者分两段各推演一次：

| 阶段 | 含义 | 失败说明 |
|---|---|---|
| `frozen_new` | **当前已部署**的消费者读 vN 的新消息 | 不能直接上线，线上会立刻出错 |
| `upgraded_backlog` | 升级后的消费者重放历史消息 | 能上线但会卡在积压上 |

于是四种判定：

- **可直接升级** `direct` —— 两段都过。
- **需双写** `dual_write` —— 只有重命名问题，且消费者是 `new_only`、还没升级。生产端同时发旧名即可，探测会验证补发后的消息确实能读。
- **必须等旧消息处理完** `wait_drain` —— 新消息能读，升级后读不了历史。先把积压排空再切。
- **不可升级** `blocked` —— 当前部署读新消息就已经失败，或者问题是双写救不了的（类型、枚举、必填）。

严重度：`info` 兼容 / `degraded` 降级（能跑但语义有损，如 `number`→`integer`、默认值不一致）/ `breaking` 不兼容。
一个变更取所有受影响对里最坏的判定；聚合判定取所有变更里最坏的。

## 三类变更的实际结论（演示数据 order.placed）

**重命名字段**（v3 `customer_id` → `buyer_id`）

- `billing-legacy` v1 `new_only` → **需双写**：`renamed_field_missing`，字段 `buyer_id → customer_id`。双写探测通过。
- `fraud-screening` v3 `backlog` → **必须等旧消息处理完**：它按 v3 名字读，历史的 v1/v2 消息里仍是 `customer_id`；双写只能修"以后发的"，修不了已入队的。
- `analytics-etl` v2 `backlog` + `follow_renames` → 兼容：沿 lineage 认旧名。
- 关键点：**lineage 让同一个字段跨重命名保持身份**，所以上面这些都是"同一个字段"，而不是"删一个加一个"。

**填默认值**（v4 `currency` 默认 `USD` → `CNY`）

- 标记为 `degraded`，`default_divergence`：消费者不传该字段时会填出与生产者不同的值。能跑，但需要对账。
- 生产者自己给可选字段加默认值（v4 `tax_amount` 加 `0`、`coupon_code` 加 `""`）→ 兼容：只是允许生产者省略。

**可选转必填**（v4 `tax_amount`、`coupon_code` 由可选变必填，同时补了默认值）

- 因为**同时声明了默认值**，判定为兼容：生产者总能给出值，消费者也不需要额外处理。
- 反例（单测里验证）：可选转必填但**没有**默认值 → `required_field_may_be_absent` / `missing_required`，`breaking`。
- 消费者可以用 `overrides` 声明一个自己的默认值把它救回来（`consumer_default_fill`），代价是值可能与生产者不同。

## 历史消息模拟

每个版本按三种合法序列化习惯各生成一条消息，再叠加库里真实录制的消息：

- `full` —— 所有字段都带上。
- `minimal` —— 省略"可选且有默认值"的字段。真实实现常这么省字节，**可选转必填就是在这里暴露的**。
- `edge` —— 枚举取最后一个成员。**枚举收窄/新增成员就是在这里暴露的**（v4 给 `payment_method` 加 `crypto`，钉在 v3 的消费者直接 `enum_violation`）。

失败报告始终带 `(生产者版本, 消费者名, 消费者版本)` 和字段级信息，形如 `amount → total_amount`，并附样例消息。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/event-types` | 列出/新建事件类型 |
| GET | `/api/event-types/{event}` | 版本、消费者、录制消息 |
| POST | `.../contracts` | 追加一个版本 |
| DELETE | `.../contracts/{version}` | 删除一个版本 |
| GET | `.../diff?from=&to=` | 两个版本的逐字段差异 |
| GET | `.../impacts?to=` | 每个变更的判定 + 受影响对 + 补救办法 |
| PUT/DELETE | `.../consumers[/{name}]` | 登记/删除消费者 |
| POST/DELETE | `.../messages` | 录入/清空真实历史消息 |
| GET | `.../matrix` | 版本 × 消费者的兼容矩阵 |
| POST | `.../simulate` | 按 `consumer_name` + `producer_versions` 推演 |
| POST | `.../release-check` | 两阶段发布判定 |
| GET | `.../releases` | 判定历史 |

## UI

四个标签页：**兼容矩阵**（哪个版本 × 哪个消费者不兼容，格子里直接列出字段）、**历史消息模拟**（逐版本逐字段的失败）、**发布判定**（四种判定分栏）、**逐项变更结论**（一个变更一条，带受影响消费者和补救办法）。

## 文件

```
event_contract_registry/contracts.py   校验、类型/时间戳判定、lineage
event_contract_registry/compat.py      比对、消息模拟、解码、发布判定
event_contract_registry/db.py          SQLite 持久化
event_contract_registry/demo.py        演示数据集（order.placed 4 版、inventory.adjusted 2 版）
event_contract_registry/server.py      HTTP API + 静态文件
event_contract_registry/static/        UI（无框架、无构建）
tests/                                 38 项单测 + DOM shim 下的 UI 渲染检查
```
