# live/ — 实盘执行层 (Phase 1 只读对账 · Phase 2 干跑签名 · Phase 3 受控写通道 · Phase 3b 执行端口)

**总原则：live 与 paper 同策略、同逻辑、同基建，只有"成交通道"不同。** 引擎 (`_r_cycle.py`) 照旧做
数据拉取 / arm-fire 判定 / 时间窗 / 共识过滤 / sleeve / leg sizing / 状态 schema / 事件 / 健康文件 /
结算记账；差异只发生在"怎么成交"这一层（Phase 3b 的 `live/port.py`）。

| 阶段 | 能力 | 写路径 | 状态 |
|------|------|--------|------|
| **Phase 1** | 只读对账：余额/授权/挂单/持仓/出口/风控预判 | **无**（`py-clob-client` 只读端点 + data-api + ipinfo.io，全 GET） | ✅ |
| **Phase 2** | 干跑签名：本地 EIP-712 签名、产物不可提交 | **无**（`create_order()` 本地签名；网络侧只有 `/book`、`/fee-rate` 等 GET） | ✅ |
| **Phase 3** | 冒烟单 + 受控提交通道（**v1 CLOB**，见下方"legacy"标注） | 有：`live/submit.py`（`post_order`/`cancel`），由冒烟单/人工触发 | ✅（**v1 订单已被 CLOB 拒**） |
| **Phase 3b** | 执行端口 + **CLOB v2** 真实通道 | 有：`live/v2_transport.py`（`post_order`/`cancel_orders`），仅引擎经端口调用 | ✅ 端口就绪，**live 未接信号** |

**写路径的静态保证（与上面这张表一致）**：`tests_live.py` 用 AST 断言**每个写通道恰好一处**下单调用点
（`submit.py` 一处 `post_order`、`v2_transport.py` 一处 `post_order`），写调用只允许出现在这两个通道模块内
（其它模块只能**委托**：`submit.submit_order(...)` / `submit.cancel_order(...)`），禁止调用"调用的结果"
（`f()()`），且禁用名单里的名字在通道内只能作为字符串/`setattr` 目标出现。

**与 engine 的关系（Phase 3b 起）**：`_r_cycle._paper_fire` 的**成交段**已改为经执行端口
（`live/port.py`，`_r_cycle.py` 仅 +31/−8）；为此 `live/port.py` **确实** import 了仓库内的
`re_execution` / `paper_capital`（paper 成交与记账），这是"同基建"的刻意选择。策略/数据/估值类模块
（`reversal_strategy.py`、`consensus_tracker.py`、`sleeve_signal.py`、`equity_valuation.py`、
`research/*`、`market_adapter.py`）**零改动**。

**第三方依赖：只有一个有效 SDK —— `py-clob-client-v2`（CLOB v2），装在仓库外的独立 venv**
`~/桌面/poly-yes2/live-probe-v2/.venv`（my155 上为 `/root/live-probe-v2/.venv`）。
本仓库仍是 stdlib-only（不加 requirements.txt），且 **v2 SDK 只被惰性导入**：paper 路径与
stdlib 单测永不加载它。

> ⚠ **v1 已废弃**：`py-clob-client`（v1，`import py_clob_client`）自 **2026-04-28** 起被 Polymarket 归档，
> **所有 v1 签名订单都会被拒**（`invalid order version`）。Phase 3b-3 起 `live/` 下**不再有任何 v1 import**；
> 旧 venv `~/桌面/poly-yes2/live-probe/.venv` 只保留给历史脚本，**不再被本层使用**。

### 各工具使用的 SDK（Phase 3b-3 后）

| 工具 | 作用 | SDK / 通道 | 网络 |
|------|------|-----------|------|
| `live/clob_client.py` | 共享 v2 门面：建 client（显式 ApiCreds）、余额/授权、挂单、`get_order`/`get_trades`、IPv4/代理/data-api 辅助 | **v2**（读）；写操作**委托** `v2_transport` | 只读调用 |
| `live/v2_transport.py` | **唯一**下单/撤单实现：三重闸门 + 夹价 + 成交对账 + 撤单重试 + 审计 | **v2** | 读 + 受控写 |
| `live/reconcile.py` | 只读对账（余额/授权/挂单/持仓/出口/风控预判） | **v2**（经 `clob_client`） | 只读 |
| `live/port.py` | 执行端口：`PaperPort` / `LivePort`（后者调 `v2_transport`） | v2（live 侧） | 引擎路径 |
| `live/sign_dryrun.py` | 干跑签名（本地 EIP-712，不提交） | **v2**（哨兵名单取自 `v2_transport`） | 只读（`--scenario` 零网络） |
| `live/smoke.py` | 冒烟单编排（操作者手动触发） | **v2**（经 `submit`→`v2_transport`） | 只读 + 受控写 |
| `live/submit.py` | 安全机制本体（闸门/审计/限额/被动性）+ v1 时代 CLI 适配器 | **v2**（写操作委托 `v2_transport`） | 只读 + 受控写 |
| `live/order_plan.py` / `live/risk_gate.py` / `live/creds.py` | 纯函数/凭据校验 | 无 SDK | 零网络 |

## 运行方式

真实链路（reconcile / smoke / 干跑 / 提交）需要 **CLOB v2** SDK，它**只存在于独立 venv**
`/home/da/桌面/poly-yes2/live-probe-v2/.venv`（my155: `/root/live-probe-v2/.venv`）；本仓库仍是 stdlib-only。
stdlib 解释器下，只读工具会以"py-clob-client-v2 is not importable …"明确报错并 exit 2（fail-closed）。

```bash
cd /home/da/桌面/poly-yes2/weatherbotyes2re

# 人类可读摘要 (默认写 data/live_reconcile.json)
/home/da/桌面/poly-yes2/live-probe/.venv/bin/python live/reconcile.py

# 机器可读
/home/da/桌面/poly-yes2/live-probe/.venv/bin/python live/reconcile.py --json
/home/da/桌面/poly-yes2/live-probe/.venv/bin/python live/reconcile.py --out /tmp/lr.json

# 单测 (stdlib，无需 py-clob-client，不联网)
python3.13 tests_live.py
```

退出码：`0` = 采集成功；`2` = 失败关闭 (凭据缺失/网络异常/未装库)。失败时报告仍会写出，
只是 `ok: false` + `reason`。

若用系统 python 跑会得到明确的报错提示，告诉你该用哪个解释器。

## 输出字段表

| 字段 | 类型 | 含义 |
|------|------|------|
| `ok` | bool | 整个采集是否成功 |
| `reason` | str\|null | 失败原因 (`异常类名: 消息`，已做密钥脱敏) |
| `ts_utc` | str | 采集时刻 (UTC, `...Z`) |
| `mode` | str\|null | `.env` 的 `YES2RE_MODE` (当前为 `live`) |
| `api_ok` | bool | CLOB 只读调用 (余额 + 挂单) 是否成功 |
| `usdc_balance` | float | 抵押品余额，由 6 位整数换算（当前抵押品是 **pUSD / Polymarket USD `0xc011a7e1…`**，非 USDC.e；同为 6 位小数，数值正确，字段名沿用旧称，Phase 3 记账前再改名） |
| `allowances` | dict | 合约地址 → 授权额度：`"max"` (≈uint256 上限) / USDC 数值 / `0` |
| `open_orders` | int | 当前挂单数 (只读 GET) |
| `positions` | list | data-api 持仓，仅保留 `currentValue > 0.1`，精简字段见下 |
| `positions_raw_count` | int | 过滤前的原始行数 (区分“确实没有”和“接口异常”) |
| `positions_value_usdc` | float | 保留持仓的 `currentValue` 合计 |
| `risk_gate` | dict | `{"allow", "reason", "detail"}`，用**真实余额** + `.env` 的 `LIVE_*` 评估 |
| `limits` | dict | `LIVE_FIRE_BUDGET_USDC` / `LIVE_MAX_OPEN_POSITIONS` / `LIVE_MAX_CAPITAL_USDC` |
| `egress_ip` / `egress_country` / `egress_org` | str\|null | 出口 IP / 国家 / ASN (ipinfo.io；失败为 `null`，不影响 `ok`) |

`positions[]` 元素字段：`asset, conditionId, outcome, outcomeIndex, size, avgPrice,
curPrice, currentValue, cashPnl, redeemable, negativeRisk, title, slug, eventSlug`。

### risk_gate 判定顺序 (先命中先返回)

| 代码 | 触发条件 |
|------|----------|
| `invalid_input` | 任一入参缺失/非数值/负数/`NaN`，或 `fire_budget_usdc <= 0` → **fail-closed** |
| `max_open_positions_reached` | `open_positions >= max_open_positions` |
| `capital_cap_exceeded` | `committed_usdc + fire_budget_usdc > max_capital_usdc` |
| `insufficient_balance` | `usdc_balance <= 0` |
| `budget_exceeds_balance` | `fire_budget_usdc > usdc_balance` |
| `ok` | 以上全过 → `allow: true` |

## 凭据与密钥

`live/creds.py` 从 `.env` 读取并**只校验格式**：私钥 `0x`+64 hex、funder `0x`+40 hex、
`POLY_SIGNATURE_TYPE ∈ {0,1,2}`、L2 三件套**全有或全无**。报错只提键名，绝不含值；
唯一的值出口是 `mask()` → `前8…后4`。`sanitize()` 会从异常文本里抹掉已知密钥值。

## 网络注意事项

- 本机需走 `.env` 的 `http_proxy`/`https_proxy`（`reconcile` 会注入进程环境，urllib 与 httpx 都会用）。
- **强制 IPv4**：本机无 IPv6 路由，DNS 返回 AAAA 时 `connect()` 直接 `ENETUNREACH`，
  因此 `clob_client.force_ipv4()` 包了 `socket.getaddrinfo` 只返回 `AF_INET`。
- 出口位置 (`egress_ip/country`) 用于确认部署在离 CLOB 近的 VPS。

## Phase 2 — 干跑下单构造 (签名但不提交)

```bash
VENV=/home/da/桌面/poly-yes2/live-probe/.venv/bin/python

# 1) 离线合成盘口干跑 (零网络: 本地 EIP-712 签名)
$VENV live/sign_dryrun.py --scenario --confirm-dryrun

# 2) 真实盘口干跑 (默认从 paper state 的 armed 会话找活跃市场; 全部只读 GET)
$VENV live/sign_dryrun.py --confirm-dryrun --city london --date 2026-09-11 --direction high \
      --budget-usdc 3
$VENV live/sign_dryrun.py --confirm-dryrun --token-id <clob token id> --budget-usdc 3   # 直接指定

# 机器可读 + 自定义落盘
$VENV live/sign_dryrun.py --scenario --confirm-dryrun --json --out /tmp/dryrun.json
```

退出码：`0` = 已签名并落盘；`2` = 失败关闭 (缺 `--confirm-dryrun` / 缺凭据 / 计划被拒 /
网络异常)。产物默认写 `data/live_order_dryrun.json`。

### 三层"不提交"自证

1. **运行时哨兵 (sentinel)** — 构造 client 之后、任何签名之前，`install_sentinels()` 把
   client 可达的写入面 (order / cancel / RFQ / credential-admin / **state-write**) 全部用
   `setattr(target, name, blocked)` 替换成抛 `RuntimeError("SUBMIT BLOCKED (dry-run)")` 的函数：
   - **order / cancel**（7 个，**必须存在，缺一个就拒绝运行**）：
     `create_and_post_order` / `post_order` / `post_orders` / `cancel` / `cancel_orders` /
     `cancel_all` / `cancel_market_orders`；
   - **credential-admin / allowance**：`create_api_key` / `derive_api_key` / `delete_api_key` /
     `create_readonly_api_key` / `delete_readonly_api_key` / `update_balance_allowance`；
   - **state-write**：`post_heartbeat`（POST `/v1/heartbeats`，可撤下全部挂单）、
     `drop_notifications`（DELETE `/notifications`）；
   - **RFQ order entry**（在 `client.rfq` 上）：`create_rfq_request` / `cancel_rfq_request` /
     `create_rfq_quote` / `cancel_rfq_quote` / `accept_rfq_quote` / `approve_rfq_order`。

   实测 `py-clob-client 0.34.6` 上共 **21 个**哨兵。覆盖率由单测保证：枚举
   `dir(ClobClient)` / `dir(RfqClient)` 中所有写类前缀 (`post_/cancel_/delete_/update_/
   create_/drop_/derive_/approve_/accept_/set_`) 的方法，断言其 ⊆ 哨兵集；只读白名单
   (`create_order` / `create_market_order` / `create_or_derive_api_creds` / `set_api_creds`)
   逐条写明豁免理由（本地签名或纯本地状态，审计 §2.3/§8 已实测）。
   该测试在 stdlib 下用 `dir()` 快照跑、在 venv 下用真实反射跑，库升级后会漂移报警。
   随后 `prove_sentinels()` **真的逐个调用一次**并把异常记进产物 `sentinel.proof`
   (每条含 `target`/`method`/`blocked`/`patched`/`error`)——是证据，不是声明。
   哨兵未全部证明被拦下时，`run_dryrun()` 直接拒绝继续。
2. **只签名** — 订单只经 `client.create_order()` (本地 EIP-712 签名；`--scenario` 走
   `py_order_utils` 的等价本地路径)。产物里 `submit.attempted=false`。
3. **产物不可提交** — 原始签名**刻意不落盘**，只留 `signature_present` / `signature_length`
   / `signature_prefix`，因此 `data/live_order_dryrun.json` 无法被任何读到它的人拿去下单。

### 产物字段

| 字段 | 含义 |
|------|------|
| `sentinel` | `installed` (`target.method` 列表) / `missing` (该库版本没有的方法) / `proof` (逐个调用被拦下的证据) / `order_submit_methods` (必须存在的 7 个) |
| `market` | 选中的真实市场 (city/date/direction/bucket/token_id/title/volume) |
| `discovery.attempts` | 市场发现过程 (Gamma 提示 + CLOB 盘口裁决，每次尝试的状态) |
| `book` | CLOB 盘口快照 (`best_ask`=min(asks)、`best_bid`=max(bids)、tick、min_order_size、neg_risk) |
| `plan` | `order_plan.plan_order` 结果 (方向/价格/股数/最大成本/tick/cap/budget) |
| `signed_order` | `hash` (EIP-712 digest) / `maker_amount` / `taker_amount` / `order` (全字段) / 签名"存在性" |
| `submit` | `attempted=false` + 哨兵清单 + 明确声明 |
| `caps` / `caps_source` | 从 `config/yes2re_reversal.json` **只读**取得的 `no_max_ask`/`yes_max_ask`；**缺键/非法即 `CapsError` → `ok:false` + exit 2**（绝不默认成"无上限"） |

### order_plan 的决策码

`ok` / `invalid_input` (缺参/非法/`min_order_size` 缺失) / `no_book` (无对手价) /
`price_above_cap` (ask 超过 cap，含对齐后仍超) / `below_min_order_size` /
`insufficient_budget`，以及 `invalid_input` 的两个边界：`budget_usdc` 超过
`MAX_BUDGET_USDC` (1e12) 或大到无法按 tick 量子量化 (避免 `decimal.InvalidOperation`)。
价格向下对齐到 tick，股数向下取整到 2 位 (可选 6 位)，`max_cost_usdc = size × price ≤ budget`，
绝不向上取整。上限语义与 paper 侧一致 (`no_max_ask`/`yes_max_ask`，只读引用，不写回)；
**上限读不到就拒绝**（`load_caps()` 缺键/非法/越界 → `CapsError` → `ok:false` + exit 2），
不会退化成"无上限"。

## Phase 3 — 冒烟单 (受控的真实提交通道 · **legacy v1 通道**)

> ⚠ **历史通道说明**：本节描述的是 **CLOB v1** 通道（`live/submit.py` + `live/smoke.py`）。
> Polymarket 自 **2026-04-28** 起迁到 v2，**v1 签名订单会被服务端拒绝**（`invalid order version`），
> 因此这两个文件现在只用于：① 承载**安全机制本体**（三重闸门 / 哨兵 / 审计 / 被动性与限额检查，
> 两者被 Phase 3b 的 v2 通道**复用**）；② 离线与小规模演练。**任何真实下单请走 Phase 3b 的 v2 通道**
> （`live/v2_transport.py`）。下文命令保留为历史与排障参考。

**性质变化**：Phase 1/2 的铁律是"写路径不可达"；Phase 3 需要一条**受控、最小**的真实写路径，
于是要求变成：**写路径不可误触发、不可超限、每一步可审计**。整个包内只有 `live/submit.py`
能下单/撤单（AST 单测断言：全包 **恰好一处** `post_order` 调用点、一处 `cancel` 调用点，
其余模块不得出现写调用；`live/smoke.py` 只能经 `submit.submit_order`/`submit.cancel_order`）。

### 操作手册（在 my155 上执行）

```bash
VENV=/home/da/桌面/poly-yes2/live-probe/.venv/bin/python     # 部署机上换成对应 venv
cd /root/weatherbotyes2re

# 0) 探针（不触网、不下单）
$VENV live/submit.py --phrase        # 打印今天的确认短语 SMOKE-YYYY-MM-DD
$VENV live/submit.py --status        # 三条闸门各自是否满足
$VENV live/smoke.py --dry-plan       # 用合成盘口打印计划（零网络、零写路径）
$VENV live/submit.py --open-orders   # 只读：当前挂单
$VENV live/submit.py --audit-summary # 只读：审计日志里到底发生过什么（submit/cancel 计数与 order id）

# 0b) 只读预演：跑完真实链路（找市场→风控→计划→限额→被动性）后在"写"之前停下
#     只读网络、不可能下单、不需要三重闸门
$VENV live/smoke.py --readonly-preflight --city london --date 2026-09-11 --direction high \
      --budget-usdc 5

# 1) 冒烟单（三重闸门全需满足）
export LIVE_SUBMIT_ENABLED=1                      # ② 刻意不写进 .env
$VENV live/smoke.py --enable-submit \
      --confirm "$($VENV live/submit.py --phrase)" # ① + ③
```

预期输出（成功）：`ok=True`、`order: id=… confirmed=live after_cancel=canceled`、
`reconcile: open_orders=0`，产物写 `data/live_smoke.json`，退出码 `0`。
审计日志 `data/live_events.jsonl` 会依次出现
`intent → sentinel_armed → discover → risk → plan → limits → non_marketable → submit → query → cancel → query → reconcile → complete`。

### 三重闸门（为什么是三个）

| # | 闸门 | 防的是什么 |
|---|------|-----------|
| ① | CLI `--enable-submit` | **误调用**：裸跑 `smoke.py`（cron/循环/复制来的只读命令）永远进不了写路径 |
| ② | 环境变量 `LIVE_SUBMIT_ENABLED=1` | **误机器/误会话**：该变量刻意**不放进 `.env`**，任何只是加载 `.env` 的进程（本机排查、测试）都仍是只读；只有显式 export 的那台机器/会话被授权 |
| ③ | `--confirm SMOKE-<UTC日期>` | **陈旧重放**：短语按 UTC 日期生成，昨天的命令行/脚本/历史记录今天必定失败，且强制操作者看一眼今天的短语 |

三者缺一即拒绝，**退出码 3**，并写一条 `gate_deny` 审计记录（拒绝也必须留痕）。
纵深防御：`submit.submit_order()` **自身**要求传入"三闸门全部通过"的记录，否则抛
`PermissionError`（连签名都不会发生）；而 `cancel_order()` 刻意**不设**此要求 ——
撤单是恢复方向，必须永远可用。

### 提交前四道检查（顺序固定，全部通过才提交）

1. `risk_gate.evaluate` — 真实余额 / 持仓数 / 已占用资金 + `LIVE_*` 上限
2. `order_plan.plan_order` — tick 对齐、股数取整、`min_order_size`、价格上限
3. 单笔名义额 ≤ `LIVE_FIRE_BUDGET_USDC` 且 已占用 + 名义额 ≤ `LIVE_MAX_CAPITAL_USDC`
4. **禁止可立即成交**：BUY 价必须 **严格低于** best_ask（SELL 严格高于 best_bid）；
   冒烟单价格 = `min(best_bid, best_ask − 2·tick)`，向下对齐到 tick，且 ≥ 1 tick；
   下单时再叠加 `post_only=True`（交易所侧 maker-only 兜底）

### 最小权限哨兵（v1 通道）

> 同样属 legacy：下列 21 个 v1 方法名对应 `py-clob-client`（v1）。CLOB v2 的写法见
> Phase 3b 小节的"最小权限 + 审计"（**25 个写面 / 释放 2 / 只读 6**）。

先按 Phase 2 装齐全部 21 个哨兵，然后**只解除** `post_order` + `cancel`（写）；
`get_order`/`get_orders`/`get_trades`/`get_balance_allowance` 是只读调用（Phase 2 从未拦截，
此处仅显式记录）。其余（全部 RFQ、凭据/授权管理、`post_heartbeat`、`drop_notifications`）
**保持拦截**，且运行时与单测都断言其仍抛 `RuntimeError`。被解除的写方法只被"恢复/记录"，
**不会被调用**（调用即真实下单）。

### 审计日志

**提交与撤单由 `live/submit.py` 自己审计**（它才是唯一的 `post_order` 调用点）：提交前的
`intent` 记录是**强制**的——写不进去就什么都不签、不提交；提交后的 `submit` 记录为尽力而为
（此时订单可能已在盘上，失败会打 stderr 并置 `audited=false`）。撤单方向相反：日志坏掉也
**不阻塞**撤单（恢复优先），只打 stderr。

每个动作（意图/拒绝/提交/查询/撤单/异常/救援）追加一行 JSON 到 `data/live_events.jsonl`：
`ts_utc` / `action` / `reason` / `params`(已脱敏，键名含 key/secret/pass/priv/signature 一律 `<redacted>`)
/ `response_summary` / `order_id`。日志写不进去 → 抛 `AuditError` 拒绝动作（没有审计就不许动手）。

**日志卫生约定（quarantine 指针）**：`data/live_events.jsonl` 只记**真实动作**。历史上线发现测试/桩
传输的残渣（order id 形如 `ORD-*`、token `TOK`，绝无真实 CLOB 单号）会被**逐字搬**到
`data/live_events.test_debris.jsonl`（**不删除**），并在**主日志**追加一条 `action=note`、
`reason=debris_pointer` 的记录指向该文件；测试侧自 `tests_live.py` / `tests_port.py` 起在 import 时
即把 `submit.AUDIT_PATH` 指向临时文件，确保套件永远不写真实日志。

### 失败时的人工处置

| 现象 | 含义 | 处置 |
|------|------|------|
| `submit_failed:*` + `residual_risk=true` | 连接断在提交中，**可能已挂上** | 立刻 `live/submit.py --open-orders`，看到同 token/价格的挂单就 `--cancel-order <id>`（仍需三重闸门） |
| `cancel_confirmed_but_list_still_shows_order` | 撤单**已确认**，但多次重读（默认 3 次 × 5s，`--open-orders-attempts/--open-orders-interval`）后挂单列表**仍只显示该 id** | 大概率是列表缓存滞留：核对网页端；确认已撤即可忽略（`open_orders_check.note = cancel_confirmed_but_list_lag` 已留痕） |
| `other_orders_remain` | 列表里出现**不是本次**的挂单 id | 属真实残留 → 按上一行处理（人工撤单/排查） |
| `cancel_not_confirmed` / `open_orders_remain` | 撤单未确认/仍有挂单 | 同上，必要时到 Polymarket 网页端手动撤 |
| `unexpected_fill:size_matched=…` | 被动单竟然成交了（不该发生） | 视为**异常事件**上报；剩余挂单会被自动撤，成交部分按真实仓位对账 |
| `verify:order_not_confirmed` | 下单后查不到 resting 状态 | 视为可能有残留挂单，按第 1 行处置 |
| 退出码 3 | 三重闸门未满足 | 检查 `--status`，确认 `LIVE_SUBMIT_ENABLED=1` 与今天的短语 |
| 默认候选项报 `best_bid: missing` | 该桶单边/已死（常见于当天已结算的桶） | 显式指定活跃盘口：`--city <city> --date <本地日期> --direction high`（或 `--token-id <id>`） |

其他参数：`--leg buy_yes|buy_no`（默认 `buy_yes`；注意 `--direction` 是**市场方向** high/low，
不是腿方向）、`--price <限价>`（覆盖被动价，若可成交仍会在第 ④ 步被拒）、`--json`、
`--out <path>`（默认 `data/live_smoke.json`）。

任何失败路径都会**尽力撤单**（先按 order_id，再按 token + 价格**数值**匹配挂单列表精确撤），
并明确标注 `residual_risk`。**绝不会盲撤**：既没有 order_id 也没有 token 范围时
`rescue_cancel()` 直接拒绝（`no_scope: refusing a blind cancel scan`）——不列单、更不撤单；
`--readonly-preflight` 也不触发任何救援（那次调用从没下过单）。

### 尚未开启的部分

**真实信号接入尚未开启**：Phase 3 只到"人工触发一次冒烟单"为止。策略信号 → 下单的自动链路
（Phase 4 放量、多城市并发、逐级放大 `LIVE_*`）**没有实现**。

引擎侧的现状以 Phase 3b 小节为准：`_r_cycle._paper_fire` 的**成交段**已改为经执行端口
（`live/port.py`），paper 模式下走 `PaperPort`（行为与改动前逐字段一致）；策略判定/时间窗/共识/
sleeve/leg sizing/状态 schema/事件/结算记账**未改**。`live/submit.py` 本身仍只由**人工**触发
（冒烟单/手工撤单），不会被引擎自动调用。

## Phase 3b — 执行端口 (port) + CLOB v2

**核心原则：live 与 paper 同策略、同逻辑、同基建，只有"成交通道"不同。** 不是另写一个 bot，也不是
在策略里加 `if live:` 分支：引擎照旧做数据拉取 / arm-fire 判定 / 时间窗 / 共识过滤 / sleeve /
leg sizing / 状态 schema / 事件 / 健康文件 / 结算记账，只把"怎么成交"抽成端口。

```
_r_cycle._paper_fire(...)                  ← 同一段 ladder / 记账 / 建档逻辑 (未改策略)
        │
        ├─ port.preflight(fire, cfg)       ← paper: 恒允许；live: 真实余额/持仓 + LIVE_* 硬上限
        ├─ port.match(leg, book, limit, shares)
        │        ├─ PaperPort : re_execution.paper_match_fak   (in-memory FAK, 逐字不变)
        │        └─ LivePort  : live/v2_transport.execute_leg  (真实 CLOB v2 下单 + 成交对账)
        └─ port.fund(state, cfg, fire, total_cost)   ← 两种模式都用 paper_capital.reserve 记同一本账
```

### 文件

| 文件 | 作用 |
|------|------|
| `live/port.py` | 端口契约 + `PaperPort` / `LivePort` + `get_port(cfg, env)` / `port_status()` |
| `live/v2_transport.py` | CLOB **v2** 传输层（由 `live/submit.py` 演进）：三重闸门 + 最小权限哨兵 + 审计日志 + 真下单/查询/撤单 + 成交对账 |

### 为什么必须迁 v2

Polymarket 已于 **2026-04-28** 迁到 CLOB v2；旧 `py-clob-client`（v1，`import py_clob_client`）已被官方归档，
**所有订单都会被拒**（`invalid order version`）。v2 SDK 是 `py-clob-client-v2`（`import py_clob_client_v2`），
装在独立 venv：`~/桌面/poly-yes2/live-probe-v2/.venv`（本机）/ `/root/live-probe-v2/.venv`（my155）。
本账户实测：`signature_type=1` (POLY_PROXY) + funder + 现有 API 凭据可被接受（`status=live`）。

v2 差异（本模块已处理）：creds 显式传入（v2 无 `create_or_derive_api_creds`）；撤单用 **`cancel_orders([id])`**
（单参 `cancel_order` 易抛 `AttributeError`）；挂单查询用 **`get_open_orders()`**（无 `get_orders`）；
`create_order()` 返回 **`SignedOrderV2` 对象**（无 `.get()`，用属性/`__dict__`）；
**提交前必须重取盘口并夹紧价格**，否则 `post_only` 被拒（`order crosses book`）；
服务端 `get_version()` 返回 **2**。

```bash
V2=/home/da/桌面/poly-yes2/live-probe-v2/.venv/bin/python
$V2 live/v2_transport.py --status        # 闸门状态 + 可释放清单（零网络）
$V2 live/v2_transport.py --version       # clob server version: 2（只读）
$V2 live/v2_transport.py --open-orders   # 只读：当前挂单
```

### 同一份 config 跑两个实例：三个 env 覆盖（Phase 3b-2）

paper 与 live 实例**共用同一个** `config/yes2re_reversal.json`，避免复制配置文件导致**策略漂移**。
只有下面三个变量可以用环境变量区分实例，其余（全部策略参数：窗口/共识/sleeve/腿比例/上限语义…）
**完全相同**，且**只读**取自那份 config：

| 环境变量 | 覆盖 `cfg[...]` | 取值 | 非法值 |
|---|---|---|---|
| `YES2RE_MODE` | `mode` | `paper` \| `live`（大小写/空白已归一） | 其它值 → `SystemExit`（明确报出变量名） |
| `YES2RE_FIRE_BUDGET_USDC` | `fire_budget_usdc` | 有限正数（支持 `12`、`7.5`、`1e1`） | 非数字/≤0/`NaN`/`inf` → `SystemExit` |
| `YES2RE_MAX_OPEN_POSITIONS` | `max_open_positions` | 正整数 | `0`/负数/小数/非数字 → `SystemExit` |
| `YES2RE_INITIAL_CAPITAL_USDC` | `paper_initial_capital_usdc` | 有限正数（**实盘实例填真实账户余额**，让"当前权益"以真实资金起算） | 非数字/≤0/`NaN`/`inf` → `SystemExit` |

规则（与 `_r_state.load_config` 实现一致）：

- **未设置（或空串）＝不覆盖**：输出与改动前**逐字段一致**（`tests_port.py` 用改动前的 golden 全量比对）。
- **非法值 → fail-closed 抛错**，绝不静默忽略（宁可启动失败，不要跑错额度）。
- 覆盖发生在 `_validate_config` **之前**，校验对**生效后的**配置照常执行（间隔/金额/模式检查都还在）。
- **安全锁保留**：**config 文件本身**永远不能把 `mode` 选成 live（`mode: live` 的文件仍被拒）；
  只有**显式**的 `YES2RE_MODE=live` 才能选中 live，且选中时会在 stderr 打一行 WARNING（journal 可见）。
  真正下单还需执行端口的三重闸门（下表），所以"mode=live 但缺闸门"仍是不开仓。
- **实盘账本起点**：live 实例用 `YES2RE_INITIAL_CAPITAL_USDC=<真实余额>` 对齐账本初始资金
  （`load_config` 覆盖 `paper_initial_capital_usdc`；仅在 state **尚无持仓/无借记**时用于初始化账本，
  之后引擎永不改写初始资金），这样镜像出去的健康/权益数值以真实资金起算，而不是配置里的 paper 值。
- 部署提示：**不要把 `.env` 当 `EnvironmentFile`**（本仓库 `.env` 里有历史遗留的 `YES2RE_MODE=live`），
  否则 paper 实例可能被环境选中 live。单元里用显式 `Environment=` 行（见 `DEPLOY_RUNBOOK.md` §7）。

### 三重闸门（服务侧版本）

引擎不是 CLI 工具，所以三个闸门换成**环境变量**（全部**刻意不写进 `.env`**）；缺任何一个：
`get_port()` 抛 `PortRefused(reason)`，`_r_cycle` 记 `fire_port_refused` 事件并**不开仓**——
**绝不静默降级成 paper**。

| # | 闸门（live 模式） | 防的是什么 |
|---|------------------|-----------|
| ① | `YES2RE_LIVE_ENABLE_SUBMIT=1` | 误调用：服务/脚本没有显式导出它就跑不了写路径 |
| ② | `LIVE_SUBMIT_ENABLED=1` | 误机器/误会话：只加载 `.env` 的进程（本机排查、测试）永远是只读 |
| ③ | `YES2RE_LIVE_CONFIRM=SMOKE-<UTC日期>` | 陈旧重放：日期短语过期即拒，强制操作者当天确认 |
| — | `cfg["mode"] == "live"` | 引擎模式本身；当前部署是 `paper`（默认），`reversal_runner.py` 亦拒绝非 paper |

### 最小权限 + 审计（沿用 v1 机制）

- 先装齐 v2 全部写面哨兵 —— **25 个方法，逐项如下（与 `live/v2_transport.py` 的四个常量一一对应）**：

  | 组 | 数量 | 方法（常量） |
  |----|------|--------------|
  | order-entry | **8** | `create_and_post_order` / `create_and_post_market_order` / `post_order` / `post_orders` / `cancel_order` / `cancel_orders` / `cancel_all` / `cancel_market_orders`（`SUBMIT_METHODS`） |
  | credential-admin / allowance | **9** | `create_api_key` / `create_or_derive_api_key` / `create_builder_api_key` / `create_readonly_api_key` / `delete_api_key` / `delete_readonly_api_key` / `derive_api_key` / `revoke_builder_api_key` / `update_balance_allowance`（`ADMIN_METHODS`） |
  | state-write | **2** | `post_heartbeat`（POST `/v1/heartbeats`，可撤下全部挂单） / `drop_notifications`（DELETE `/notifications`）（`STATE_WRITE_METHODS`） |
  | RFQ | **6** | `create_rfq_request` / `cancel_rfq_request` / `create_rfq_quote` / `cancel_rfq_quote` / `accept_rfq_quote` / `approve_rfq_order`（`RFQ_SUBMIT_METHODS`） |
  | **合计** | **25** | 8 + 9 + 2 + 6 |

  然后**只放出 2 个写方法**（`RELEASE_WRITE_METHODS`）：`post_order` + `cancel_orders`；其余 **23** 个保持拦截
  （真实 client 实测 `still_blocked_count = 23`）。另放行 6 个**只读**方法（`RELEASE_READ_METHODS`，
  Phase 2 从未拦截过）：
  `get_order`/`get_open_orders`/`get_trades`/`get_balance_allowance`/`get_order_book`/`get_tick_size`）。
  注意 `cancel_order`（单参 `DELETE /order`）**装哨兵但永不放出**：安全形式是 `cancel_orders([id])`；
  想手工撤单请用 `live/submit.py --cancel-order <id>` 或 `live/v2_transport.py --cancel-order <id>`。
- 闸门 / 审计（`data/live_events.jsonl`，含拒绝）/ `check_non_marketable` / `check_limits` 全部**复用**
  `live/submit.py` 的实现（同一函数对象），v2 不另起一套。
- 下单：`post_only=True` + **提交前重取盘口夹紧**（BUY ≤ `best_ask − tick`、SELL ≥ `best_bid + tick`，
  仍须被动）；下单**不重试**（重试会双开）。
- 成交对账：轮询 `get_order` + `get_trades` 至终态或超时，把**真实成交量/均价**回报给引擎；
- 撤单：`cancel_orders([id])` **失败重试 3 次**，仍失败 → 结果带 `residual_risk=True` 并写审计。

### 已知限制（Phase 3b）

1. **未接真实信号**：`cfg["mode"]` 仍是 `paper`，三个闸门也未配置；live 端口已就绪但不会被自动触发。
2. 真实成交的**手续费/返佣**未入账（`paper_capital` 只记成本）；Phase 4 需要时按 `get_trades` 的
   `fee_rate_bps` 扩展。
3. `poll_fill` 的均价优先取 `get_trades`（按 orderID 归属），取不到时退化为订单限价。
4. 部分成交后剩余量的撤单依赖 `cancel_orders`；服务端极端情况下可能已成交（`post_only` 下概率极低），
   此时 `residual_risk` 会明确标注。
5. paper 与 live 共用同一本账（`paper_total_debit_usdc`）——live 模式下它就是真实支出账本；
   两模式**不要混跑**同一份 state。

## 阶段梯子

| 阶段 | 内容 | 允许的动作 | 当前状态 |
|------|------|-----------|----------|
| **Phase 1** | 只读对账：余额/授权/挂单/持仓/出口/风控预判 | 只有 GET | ✅ 本包实现 |
| **Phase 2** | 干跑签名：真实构建订单并本地签名，**不提交**；哨兵 + 产物不可提交 | 本地签名 + 只读 GET | ✅ 本包实现 |
| **Phase 3** | 冒烟单：5 USDC 非可成交限价单，人工触发 → 查单 → 撤单 → 对账 | 三重闸门 + 最小权限写 | ✅ 本包实现（真实下单由操作者在 my155 触发） |
| **Phase 3b** | 执行端口 + CLOB v2：live/paper 同策略同逻辑同基建，仅成交通道不同 | 端口选择（三闸门拒则不开仓） | ✅ 本包实现（live 未接信号） |
| **Phase 4** | 放量：多城市并发、逐级放大 `LIVE_*` 上限 | 常态实盘 | 待做 |

升级闸门（每一级都必须满足才进下一级）：Phase 1 连续 N 天 `ok=true` 且余额/挂单与人工
对账一致；Phase 2 干跑签名与 CLOB 校验一致；Phase 3 最小单成交/费用/结算与对账逐值吻合。
