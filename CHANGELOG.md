# Changelog — weatherbotyes2re

## 2026-09-11 — 运维文档（ops/PENDING.md 待办单一清单）+ LIVE 真实验收记录

- 新增 **`ops/PENDING.md`**：双实例全貌（paper / LIVE）、**4 个待决策项**（LIVE 闸门开启 / config 600 vs 账本 500 / `miami no_book` 永久锁是否重试 / LIVE 结算 claim 未实现）、**5 个 LIVE 技术待办**（账本初始资金对齐真实余额 / my155 WS down / 撤单后列表延迟 / 真实订单运维流程含事故复盘 / 资金规模）、paper 历史遗留、环境变量对照表、安全红线。
- **文档准确性修正**：引擎侧真实下单需**三个服务级闸门同时满足** —— `YES2RE_LIVE_ENABLE_SUBMIT=1` + `LIVE_SUBMIT_ENABLED=1` + 当日 `YES2RE_LIVE_CONFIRM`（`SMOKE-<UTC 日期>`）；日期短语跨 UTC 零点自动失效需重启单元（刻意设计，防止无人值守长期放量）。`DEPLOY_RUNBOOK` §7 状态与镜像实现说明同步更新。
- **LIVE 真实验收**（my155，真实资金）：用**本仓库工具** `live/smoke.py` 完成 `下单 → 确认(live, 0 成交) → 撤单(CANCELED)`，复查挂单 0、余额 51.713622 USDC 未变（订单 `0x505a3835…`，BUY 18.51 股 @0.27 = 4.9977 USDC，post-only 不可成交价）。
- **事故复盘（已处置）**：验收期间一次 vibeshell 会话 kill 误杀前台冒烟进程 → 20 股 @0.25 真实挂单滞留 ~1 分钟，由 `/root/live-probe-v2/cancel.py` 手工撤销（余额未变）。**规则**：真实订单流程必须 `setsid nohup … &` 独立运行（已写入 `ops/PENDING.md` §T-4）。
- `ops/repair_log_live.md` 建立（LIVE healer 首轮记录：只读账户层断裂 + ws_down 根因定位）。


## 2026-09-10 — Phase 3b-4 收尾: 冒烟对账抗列表缓存滞后 + 实盘初始资金 env 覆盖

- `live/smoke.py`：撤单确认后对挂单列表做**有限重读**（`--open-orders-attempts` 默认 3 ×
  `--open-orders-interval` 默认 5s；函数参数 `open_orders_attempts`/`open_orders_sleep`），
  新增 `await_no_open_orders()` 把四种结局分开：列表在重读中清空（若确曾滞后 → 记
  `open_orders_check.note = cancel_confirmed_but_list_lag` 供人工确认，**不报** residual risk）；
  撤单已确认但重读后**仍只显示该 id** → `cancel_confirmed_but_list_still_shows_order`（真残留风险，
  原因明确）；列表出现**别的** id → `other_orders_remain`；撤单未确认仍见挂单 → `open_orders_remain`。
  人类摘要与审计新增 `open_orders` 步骤（含 attempts/remaining/note）。
- `_r_state.load_config`：新增**第四个**环境变量覆盖 `YES2RE_INITIAL_CAPITAL_USDC=<正数>` →
  `cfg['paper_initial_capital_usdc']`（实盘实例用它把账本初始资金对齐真实账户余额，
  使 live 侧"当前权益"以真实资金起算）。缺省/空串 ⇒ 不覆盖（输出与改动前**逐字段一致**，
  由改动前 golden 全量比对守住）；非法值（非数字/≤0/`NaN`/`inf`）⇒ `SystemExit` 并报出变量名
  （fail-closed）；覆盖仍在校验之前、`mode` 安全锁与策略参数不受影响。
- 文档：`live/README.md`（env 表新增一行 + 实盘账本起点说明 + 失败处置表新增两行）、
  `DEPLOY_RUNBOOK.md`（§7.2 单元示例与 §7.3.1 差异面表新增该变量）。
- 测试：`tests_live.py` **50/50**、`tests_port.py` **16/16**（含新增：列表滞后两次后清空不报残留、
  持续显示该 id 必报残留且原因明确、别的 id 立即报残留、`await_no_open_orders()` 五分支；
  `YES2RE_INITIAL_CAPITAL_USDC` 生效/非法 fail-closed/未设置逐字段一致/账本初始化）。
- paper 回归：5 套件对基线**逐行 diff 为空**。

## 2026-09-10 — Phase 3b-3: live 层剩余工具统一迁到 CLOB v2（v1 全废弃）

- **背景**：Phase 3b 只把新的 `v2_transport` 迁到 `py-clob-client-v2`，旧工具仍 `import py_clob_client`（v1）
  ⇒ 在只有 v2 venv 的 my155 上 `live/reconcile.py` 直接 `No module named 'py_clob_client'`（exit=2），
  且对已迁移的 CLOB 后端已无意义。
- `live/clob_client.py` → **v2 门面**：`build_client`（显式 `ApiCreds`，v2 无 `create_or_derive_api_creds`）、
  `get_balance_allowance`、`get_open_orders`、`get_order`、`get_trades`、`force_ipv4`/`http_json`/
  `fetch_positions`/`fetch_egress_info`、`sdk_available()`；写操作 **委托** `submit_order`/`cancel_orders`
  到 `v2_transport`（**不产生第二份实现**，全包仍只有一个 `post_order` 调用点）。惰性导入 + v2 venv 提示。
- `live/reconcile.py` **恢复可用**（只读硬保证不变：AST 断言其零写调用，仍只用
  `build_client`/`get_balance_allowance`/`get_open_orders`/`fetch_positions`/`force_ipv4`）。
- `live/submit.py`：保留**安全机制本体**（三重闸门/审计/限额/被动性/CLI），写操作改为**适配器**委托
  `v2_transport.execute_leg` / `cancel_with_retry`；`RELEASE_WRITE_METHODS` 改为 v2 名单
  (`post_order`/`cancel_orders`)；取消单参 `cancel_order`。
- `live/sign_dryrun.py`：哨兵名单与安装**取自 `v2_transport`**（单一来源，v1 名单删除）；签名改用 v2
  `create_order`，`--scenario` 走 v2 本地 `OrderBuilder`（零网络）；order hash 用 v2
  `ExchangeOrderBuilderV2`（EIP-712 域为 `exchange_v2`/`neg_risk_exchange_v2`）；产物**仍不落盘 signature**。
- `live/v2_transport.py`：哨兵工厂与名单成为唯一来源；`take_down_unfilled` 参数（引擎路径默认 True = 立即撤余量；
  冒烟路径 False = 让单子挂着以验证 place→query→cancel 闭环）；撤单审计改为 best-effort（恢复方向不被日志阻塞）
  并在结果里回报 `audited`；`_book_from_summary` 兼容 v2 的 **dict** 盘口返回（v2 `get_order_book` 返回 dict）。
- 兼容性修正：`live/*.py` 脚本模式（`python3.13 live/xxx.py`）设置 `__package__ = "live"`，使惰性相对导入可用。
- 测试：`tests_live.py` 47/47、`tests_port.py` 16/16（stdlib 与 v2 venv 各一遍）；静态守卫更新为
  "全包恰一处 `post_order`（v2）、零 `cancel()`、`create_order` 仅 `v2_transport`/`sign_dryrun`"，
  覆盖面测试改为枚举 **v2** `dir(ClobClient)`（24 个写方法 + 3 条白名单理由）。
- 验证：v2 venv 下 `reconcile.py` / `smoke.py --readonly-preflight` / `sign_dryrun.py --scenario` /
  `submit.py --open-orders` / `v2_transport.py --version|--open-orders` 全部 exit 0（`clob server version: 2`、
  `open orders: 0`）；stdlib 下只读工具以明确提示 fail-closed（exit 2）；paper 回归 5 套件逐行 diff 为空。

## 2026-09-10 — Phase 3b-2: 部署基建 — 同一份 config 跑 paper/live 两个实例 (`_r_state.py`)

- `_r_state.load_config(path, *, env=None)` 支持**三个**环境变量覆盖（`env=None` ⇒ `os.environ`）：
  `YES2RE_MODE=paper|live` → `cfg['mode']`；`YES2RE_FIRE_BUDGET_USDC=<正数>` → `cfg['fire_budget_usdc']`；
  `YES2RE_MAX_OPEN_POSITIONS=<正整数>` → `cfg['max_open_positions']`。**只**这三个，意在让 my155 的
  paper 与 live 实例共用**同一份** `config/yes2re_reversal.json`，杜绝复制配置文件造成策略漂移。
- 语义：① 未设置/空串 ⇒ 不覆盖，输出与改动前**逐字段一致**（`tests_port.py` 用改动前 golden 全量比对）；
  ② 非法值（非数字/≤0/`NaN`/`inf`/非正整数/非法 mode）⇒ `SystemExit` 并**报出变量名**，fail-closed 不静默忽略；
  ③ 覆盖发生在 `_validate_config` **之前**，间隔/金额/模式校验对生效后的配置照常执行；
  ④ 新增 `_env_overrides()`（纯函数、可注入 env）与 `ENV_*` 常量；选中非 paper 时 stderr 打 WARNING。
- **安全锁保留并细化**：config **文件**仍然无法把 `mode` 选成 live（`_validate_config(..., mode_opt_in=False)`
  默认不变，`mode: live` 的文件照旧被拒）；只有**显式** `YES2RE_MODE=live` 才能选中，且首选还需执行端口的三重闸门
  （`tests_port.py` 断言"仅 mode=live、无闸门"→ `PortRefused(submit_flag_missing)`，不开仓、不降级）。
- 文档：`live/README.md` 新增"同一份 config 跑两个实例：三个 env 覆盖"小节；`DEPLOY_RUNBOOK.md` §7.2/§7.3.1
  更新 systemd 单元（三个 `Environment=` 行）并警示**不要把 `.env` 当 `EnvironmentFile`**（其中历史遗留的
  `YES2RE_MODE=live` 若被导出会让 paper 实例被环境选中 live）。
- 回归：`tests_reversal` / `tests_fill_gate` / `tests_sleeve_signal` / `tests_sleeve_wiring` /
  `paper_reversal_sim --scenarios-only` 五套件对基线**逐行 diff 为空**；`tests_port.py` 16/16、`tests_live.py` 47/47。

## 2026-09-10 — LIVE 执行层 Phase 3b: 执行端口(port)化 + CLOB **v2** 迁移 (`live/port.py`, `live/v2_transport.py`)

- **端口模型（操作者最高优先级：live 与 paper 同策略/同逻辑/同基建）**：`_r_cycle._paper_fire` 的
  "成交"段改为经 `live/port.py` 的端口；策略判定/时间窗/共识过滤/sleeve/leg sizing/状态 schema/
  事件/健康文件/结算记账**一行未动**（`_r_cycle.py` 仅 +31/−8，限于 import 与 fire 执行段）。
  - `PaperPort`：直接调用现有 `re_execution.paper_match_fak`；`LivePort`：经 v2 传输真下单 + 成交对账。
  - `fund()` 两模式共用 `paper_capital.reserve`（同一本账）。
  - `get_port(cfg, env)` 按 `cfg["mode"]` 选择；**live 缺任一闸门 → `PortRefused` + `fire_port_refused`
    事件 + 不开仓，绝不静默降级成 paper**（单测断言）。
- **CLOB v2（2026-04-28 起 v1 订单全被拒）**：新增 `live/v2_transport.py`（由 `live/submit.py` 演进）——
  creds 显式传入、`cancel_orders([id])`（单参 `cancel_order` 易抛 AttributeError，实测已避开）、
  `get_open_orders()`、`SignedOrderV2` 对象取值、**提交前重取盘口并夹紧价格**（否则 post-only 被拒
  `order crosses book`）、成交对账轮询 `get_order`/`get_trades` 取**真实成交量/均价**、
  **撤单失败重试 3 次仍失败即标 `residual_risk`**。v2 依赖**惰性导入**（paper 路径永不需要）。
- **安全机制全部复用而非另起**：三重闸门（服务侧换成 `YES2RE_LIVE_ENABLE_SUBMIT=1` +
  `LIVE_SUBMIT_ENABLED=1` + `YES2RE_LIVE_CONFIRM=SMOKE-<日期>`，均不写进 `.env`）、
  `data/live_events.jsonl` 追加式审计（含拒绝）、`check_non_marketable`/`check_limits` 同一函数对象、
  最小权限哨兵 —— v2 全写面 **25** 个方法（order-entry 8：`create_and_post_order`/`create_and_post_market_order`/
  `post_order`/`post_orders`/`cancel_order`/`cancel_orders`/`cancel_all`/`cancel_market_orders`；
  credential-admin 9：`create_api_key`/`create_or_derive_api_key`/`create_builder_api_key`/`create_readonly_api_key`/
  `delete_api_key`/`delete_readonly_api_key`/`derive_api_key`/`revoke_builder_api_key`/`update_balance_allowance`；
  state-write 2：`post_heartbeat`/`drop_notifications`；RFQ 6：`create_rfq_request`/`cancel_rfq_request`/
  `create_rfq_quote`/`cancel_rfq_quote`/`accept_rfq_quote`/`approve_rfq_order`），
  仅放出 **2 个写方法** `post_order` + `cancel_orders`（其余 23 保持拦截）+ **6 个只读方法**
  `get_order`/`get_open_orders`/`get_trades`/`get_balance_allowance`/`get_order_book`/`get_tick_size`；
  `cancel_order`（单参 `DELETE /order`）**装哨兵但永不放出**。清单与 `live/v2_transport.py` 的
  `SUBMIT_METHODS`/`ADMIN_METHODS`/`STATE_WRITE_METHODS`/`RFQ_SUBMIT_METHODS`/`RELEASE_*` 常量逐项一致。
- **回归**：`tests_reversal` / `tests_fill_gate` / `tests_sleeve_signal` / `tests_sleeve_wiring` /
  `paper_reversal_sim --scenarios-only` 五份基线**逐行 diff 为空**；`_paper_fire` 对固定输入与
  改动前实现（`git show HEAD:_r_cycle.py`）产物**逐字段一致**（golden 值已固化进 `tests_port.py`）。
- `tests_port.py`（新增，stdlib）：端口选择矩阵（含"缺闸门不降级"）、paper 回归、stdlib 隔离
  （无 v2 SDK 也能 `import _r_cycle` 并拿到 PaperPort）、live 端口离线桩（部分成交/未成交/撤单失败/
  超时/未 preflight）、v2 夹价/`execute_leg` 端到端/撤单重试/响应形状容错/哨兵最小权限。
- 文档：`live/README.md` Phase 3b 小节 + `DEPLOY_RUNBOOK.md` §7（my155 实盘 systemd 单元镜像
  `yes2re-paper`、观测镜像方案：复用同一套 observer/triage 与 30min cron，汇报仍含 ①开仓 ②当前权益）。
- **开发期间零真实订单**：`data/live_events.jsonl` 无 submit/cancel 动作；v2 只读复核
  `--version` → `clob server version: 2`、`--open-orders` → `open orders: 0`。
- **独立审计**（herdr impl+audit）：首轮 NEEDS_FIX → 修复 **F1**（写通道只查 `gates["ok"]`，未要求三个
  checks 全真 → 抽出 `submit.gates_all_passed()` 供 v1/v2 共用，伪造/截断的闸门记录一律拒绝）、
  **F2**（v2 `cancel_order`（DELETE `/order`）既未装哨兵也未释放、且不在 AST denylist → 纳入
  `SUBMIT_METHODS` 保持拦截，静态守卫覆盖 `cancel_order`，文档清单同步更正）、
  **F3**（`live_deps_missing` 分支不可达 → `get_port` 探测 `v2_transport.sdk_available()`，缺 SDK 时
  以该 reason 拒绝）、**F4**（README/`live/__init__.py`/`--status` hint 的过期或误导措辞更正）→ **Delta 复审 APPROVE**。
- **增量复审收尾（文档一致性，3 项 LOW）**：`live/README.md` 开头块重写（补齐 Phase 1/2/3/3b 能力表，
  删掉"任何阶段都不存在提交订单代码路径""paper 引擎一行未改""本包完全独立(不 import paper 模块)"
  "第三方依赖只有一个"等过期断言；改为与实际一致的端口模型 + 两 SDK/两 venv 说明）；
  写面清单/数量与 `live/v2_transport.py` 常量**逐项对齐**（25 / 释放 2 / 只读 6，列出每个方法名）；
  Phase 3 小节标注为 **legacy v1 通道**（2026-04-28 起 v1 订单被拒，安全机制本体被 v2 复用）；
  审计日志 quarantine 指针（主日志 `action=note`,`reason=debris_pointer` → `data/live_events.test_debris.jsonl`）。

## 2026-09-10 — LIVE 执行层 Phase 3: 受控真实提交通道 + 冒烟单 (`live/submit.py`, `live/smoke.py`)

- `live/submit.py` — 真实提交路径，受**三重闸门**保护：CLI `--enable-submit` **且** 环境变量 `LIVE_SUBMIT_ENABLED=1` **且** `--confirm SMOKE-<UTC 日期>`（防重放短语由 `--phrase` 打印）；缺一即拒（exit 3）**并写审计**。
- 提交前置检查链：`risk_gate`（真实余额/持仓/预算）→ `order_plan`（tick 对齐/股数/上限）→ 名义额 ≤ `LIVE_FIRE_BUDGET_USDC` 且累计暴露 ≤ `LIVE_MAX_CAPITAL_USDC` → **只允许 non-marketable 限价单**（BUY 价 < best_ask、SELL 价 > best_bid，违反即拒）+ `postOnly=True` 交易所侧兜底。参考价非有限值（NaN/Inf/负）→ fail-closed。
- **最小权限解除哨兵**：仅放出 `post_order`、`cancel` + 4 个只读方法（`get_order`/`get_orders`/`get_trades`/`get_balance_allowance`）；RFQ 六方法、凭据管理五方法、`post_heartbeat`、`drop_notifications` **保持拦截**（单测断言）。
- **审计**：`data/live_events.jsonl` 追加式记录每个 intent / 拒绝 / 提交 / 查询 / 撤单 / 异常（**拒绝也记录**，禁止静默跳过）；`submit.py` 自身写审计，任一到达 `post_order` 的调用都伴随审计行。
- `live/smoke.py` — 冒烟单编排（操作者用）：选取活跃桶 → 5 USDC non-marketable 限价买单 → 轮询确认挂单 → 撤单 → 确认 cancelled → 对账回到 0 挂单。失败救援**必须有范围**（`no_scope` 守卫：无 `order_id` 且无 `token_id` 时拒绝盲扫；readonly 模式不救援），撤单匹配按**数值**比较（`"0.5"` == `"0.50"`）。
- **首次真实运行（my155）暴露选桶缺陷**：discover 选中了一个死桶（`best_bid=None`、`best_ask=0.001`）→ `order_plan:no_book` 正确 fail-closed（exit 2、零提交）。修复：选桶必须**双边有盘口且近价**（`best_bid>0` 且 `0<best_ask<1` 且 ask 侧可成交量 ≥ `min_order_size`），合格桶中以 `|best_ask-0.5|` 最小者优先（并列取成交量高者）；无合格桶 → `no_tradeable_bucket` 并**逐条列出被拒候选及原因**（不允许静默跳过）。Delta 复审 APPROVE（4 组变异验证）。
- **独立审计**（herdr impl+audit，对抗性）：首轮 NEEDS_FIX → 修复 **F-A（HIGH：救援路径在计划未生成时可盲撤账户全部挂单）** / F-C（字符串价格比较致精确撤单静默失效）/ F-B（参考价 NaN 抛异常而非 fail-closed）/ F-D（提交通道自身不写审计）→ **Delta 复审 APPROVE**（4 项全 CLOSED，6 组变异验证）。
- **开发期间零真实订单**：`data/live_events.jsonl` 无任何 submit/cancel 动作、实时挂单 0、余额 51.713622 USDC 未变。真实冒烟单由操作者在 my155 上亲自触发（`min_order_size=5 股` 已实测）。


## 2026-09-10 — LIVE 执行层 Phase 2: 干跑签名（绝不提交）(`live/order_plan.py`, `live/sign_dryrun.py`)

- `live/order_plan.py` — 纯函数（stdlib）：腿意图 + 盘口 + 预算 + 价格上限 ⇒ 可签名订单参数。tick 向下对齐、股数取整、`below_min_order_size` / `price_above_cap` / `insufficient_budget` / `no_book` / 非法输入（负价、NaN、None、超大值）一律 **fail-closed**；价格上限**只读**取自 `config/yes2re_reversal.json`，缺键即 `CapsError`（不得默认放行成无上限）。
- `live/sign_dryrun.py` — 干跑器：先用**运行时哨兵**把 client 全部写入面替换为抛 `RuntimeError` 的函数（order / cancel / RFQ / credential-admin / state-write，共 **21** 个方法，含 `post_heartbeat`=POST `/v1/heartbeats`、`drop_notifications`=DELETE `/notifications`），再用 py-clob-client `create_order()` 仅做 **EIP-712 本地签名**；必须显式 `--confirm-dryrun`；产物 `data/live_order_dryrun.json`（含哨兵证明，不含任何密钥）。
- **取证**：socket 级出网 trace 证明真实网络干跑下**零非 GET 请求**；21 个哨兵逐个实测拦截；审计方独立枚举 `dir(ClobClient)` 写方法集合做覆盖率对比。
- **独立审计**（herdr impl+audit，对抗性）：首轮 NEEDS_FIX → 修复 F3（超大有限预算触发 `decimal.InvalidOperation` 抛出）/ F4（哨兵遗漏两个真实写端点）/ F5（缺 cap 键静默返回 1.0 = 无上限）→ **Delta 复审 APPROVE**。
- `tests_live.py` 扩至 **24** 项全绿（含哨兵承重性与覆盖面断言、变异验证）。


## 2026-09-10 — LIVE 执行层 Phase 1: 只读对账层 (`live/`)

- **新增完全独立的只读 live 侧模块**（主引擎仍硬性 paper-only；未改动任何现有模块、config 值或服务）：
  - `live/creds.py` — `.env` 凭据读取 + 格式校验（仅掩码输出，绝不打印全值）
  - `live/clob_client.py` — `py-clob-client` 惰性薄封装（依赖缺失时给出明确指引）
  - `live/risk_gate.py` — 纯函数风控闸门（fail-closed，机器可读 reason 码）
  - `live/reconcile.py` — 只读对账 CLI（余额 / 授权 / 挂单 / 持仓 / 风控评估 / 出口 IP），产出 `data/live_reconcile.json`
  - `tests_live.py` — 14 项单测（含"无下单调用"静态断言、非 mapping env 与非法值契约用例）
  - `live/README.md` — 运行方式 + 阶段梯子（只读对账 → 干跑签名 → 最小单 → 放量）
- **硬保证**：`live/` 内零下单/撤单/授权写入路径（静态扫描 + 运行时出网 trace 双验，全部 GET）；fail-closed（异常 ⇒ `ok:false` + exit 2）；密钥卫生（正常与异常路径输出全量 grep 零命中）。
- **首次实跑核账**（本机走 7890 代理、my155 直连，两路径结果一致）：USDC(pUSD) **51.713622**、4 个合约 allowance = uint256-max、挂单 0、79 条历史仓 `currentValue` 全 0（无活跃持仓）。
- **风控参数（操作者拍板）**：`LIVE_MAX_CAPITAL_USDC=50` / `LIVE_MAX_OPEN_POSITIONS=10` / `LIVE_FIRE_BUDGET_USDC=5`（10×5=50，与资金上限自洽）。
- **独立审计**（herdr 双 agent：impl + audit，对抗性立场）：**APPROVE**（含变异测试、链上独立复核 pUSD 余额、工作树独立性核对）；F1 契约瑕疵已闭合，F2 一行硬化同批修复。
- **工程卫生**：`.gitignore` 增加 `.env*`——此前 `.env.bak.<ts>` 备份（含私钥）未被忽略，存在误提交风险。
- **依赖说明**：live 侧第三方依赖仅 `py-clob-client`，装在独立 venv（本机 `~/桌面/poly-yes2/live-probe/.venv`、my155 `/root/live-probe/.venv`），仓库自身仍保持 stdlib-only。

## 2026-09-09 — Same-session double fire (追火) with symmetric NO+YES legs (da12518)

- **A market key (city|date|direction) may now fire at most TWICE per day.**
  Warsaw 2026-09-09 LOW 17→16→15 double break (operator decision, 2026-09-09):
  fire #1 bought YES on 16°C when the reference broke 17→16; temperature then
  fell through to 15°C and the 16°C YES was headed to zero with no further
  action possible under the old one-shot `already_fired` lock.
  New rule:
  - Fire #1 unchanged. After it, the session stays eligible for ONE 追火 when a
    fresh obs (age ≤ 180s) breaks ONE bucket past the YES bucket fire #1
    actually bought (reference — TAF or consensus rank-1 — having ratcheted
    onto that bucket first), with every gate identical to fire #1: jump=1,
    local fire window (HIGH 13-17 / LOW 1-9), consensus filter, YES price gate
    `(yes_min_ask, yes_max_ask]`.
  - 追火 leg structure is symmetric with fire #1: `buy_no_broken` on the
    newly-broken bucket (= the bucket fire #1 holds YES in; its NO is priced
    ~1 by then — placed per operator decision, cap 1.0) + `buy_yes_new` on the
    new bucket YES.
  - On 追火 fill, fire #1's old-bucket YES leg is sold at best_bid (shared
    `close_leg_at_best_bid` in `paper_capital.py`, same mode as sleeve-timeout
    closes; no bid → written off at 0, the new NO leg hedges).
  - Breaks past fire #2 take no further action (`fires >= 2` → `already_fired`).
  - `fires` counter persisted in each `fired` record; legacy records (no
    `fires` field) migrate as "1 used" — an old fired key may still 追火 once,
    but eligibility requires an open un-settled YES leg with shares, so pure
    lock/no-fill records stay inert.
  - Events: `fire` carries `fire_no` 1/2; new `close_old_yes` event logs
    shares / bid_at_close / proceeds_usdc / loss_usdc for the liquidation.
- Verified: `tests_reversal.py` 24/24 PASS (incl. refire_above_cap,
  refire_out_of_window, refire_persists_across_restart, warsaw-style
  two-branch close scenarios), sleeve 13+4, `tests_fill_gate.py` 6,
  `paper_reversal_sim.py --scenarios-only` exit 0. Dual herdr agent review
  (impl + independent audit) APPROVED. Deployed 2026-09-09 21:3x CST.
- Deployed on 192.168.1.98 (本机) with the account still at 500 USDC paper
  (config initial capital intentionally 600 per operator — takes effect on the
  next blank-state rebuild; the live guard never rewrites a trading account).

## 2026-09-09 — YES/NO leg fill floor; TAF AMD fix; market-ref fire gate; rules-refresh hardening (f5318c7)

- **Optional YES-leg fill floor** (`plan_leg_attempts`): ladder rungs are
  skipped while `best_ask <= leg.floor` (breakout not yet confirmed), aborted
  above cap as before, traded only inside `(floor, cap]`.
  `re_execution.py` + `tests_fill_gate.py`. Config: `yes_max_ask` 1.0 → 0.9,
  new `yes_min_ask` 0.48. (YES bought at ~0.91 / 0.945 / 0.99 in the 9/8–9/9
  fires was structurally too expensive — gate now caps the entry.)
- **TAF AMD/COR/RTD key fix** (`research/common.py` `checkwx_taf`): amended
  TAFs like `TAF AMD EGLC ...` were keyed under `out["AMD"]`, silently losing
  the TAF reference for that airport (London case: reference fell back to
  market rank-1 and the bot traded against the market). Corrector markers are
  now skipped and the real ICAO is always the key; multi-line TAF bodies
  handled.
- **`taf_no_extreme` event**: a TAF with no parseable TX/TN now logs a
  rate-limited per-ICAO event instead of failing silently (fail-closed
  semantics unchanged).
- **Market-ref fire gate**: `allow_market_ref_fire` config key makes
  fire-on-market-rank-1-reference optional; TAF-sourced sessions unaffected.
  HIGH direction gained an upper local-hour bound (late-evening off-window
  fires suppressed).
- **Rules-refresh failure hardening** (`_r_cycle.refresh_rules`, 2026-09-08
  KR-egress 451 incident): a full-failure round NEVER wipes the previously
  good rules index (old rules stay usable; caller filters non-today dates) and
  cold-start failures back off 120 s instead of storming Gamma every cycle
  (~6 900 wasted refreshes observed 2026-09-08). Prior-art reference:
  polymarket-market-data skill gamma-refresh-failure-diagnosis.
- Verified: `tests_reversal.py` incl. `market_ref_fire_allowed` /
  `high_late_evening_skip`, sleeve tests, fill-gate 6 — all green. Deployed
  with the double-fire change.

## 2026-09-08 — Fire-window intervalization (HIGH 13-17 / LOW 1-9 local)

- **`reversal_strategy.py` fire window switched from single-edge bounds to
  inclusive local hour intervals.** One-bucket reversal fires now gate on:
  - HIGH: local `13 <= hour <= 17` (was `hour >= 14`, no upper bound)
  - LOW: local `1 <= hour <= 9` (was `hour <= 10`, no lower bound)
- Constants: `HIGH_FIRE_LOCAL_HOUR` / `LOW_FIRE_LOCAL_HOUR_END` removed →
  `HIGH_FIRE_LOCAL_START=13` / `HIGH_FIRE_LOCAL_END=17` /
  `LOW_FIRE_LOCAL_START=1` / `LOW_FIRE_LOCAL_END=9`. `hour_ok` now takes the
  four window bounds and enforces `start <= h <= end` per direction. Both `arm`
  and the pre-fire `hour_not_in_window` gate use the same window. `prune`
  low-zombie sweep follows the new low end (9).
- Config keys: `high_fire_local_hour` / `low_fire_local_hour_end` →
  `high_fire_local_start` / `high_fire_local_end` /
  `low_fire_local_start` / `low_fire_local_end` (`config/yes2re_reversal.json`
  updated; old keys removed).
- **Rationale:** the daily extreme (and the capped peak-tick reversal this
  strategy sells) forms inside the window, not outside it. Real losses from
  out-of-window fires: mexico-city low 02:02 (LOST), SF 9/7 01:00 local fire
  (open, floating underwater) — both broke the reference at hours the peak
  window does not span. Fires observed off-window are now suppressed.
  Direction-specific windows also stop one city's LOW-break drift from firing
  into the afternoon or a HIGH from firing predawn. On-window losses of the
  chengdu class are a separate open question (bucket-break confirmation) and
  are not addressed by this change.
- Verified: `python3 tests_reversal.py` 16/16 PASS before and after (all 16
  scenarios keep passing under the interval semantics); window-boundary
  assertion (high 12/18 rejected, 13..17 accepted; low 0/10 rejected, 1..9
  accepted) green.

## 2026-09-07 — F-market unit audit + boundary-confirmation margin

- **Polymarket unit rules audited & documented** (`research/common.py`
  `c_to_market_unit` docstring):
  - Buckets: US cities 1-2°F integer buckets; EU/Asia cities 1°C buckets.
  - Resolution: Wunderground station "Daily Observations" — finalized daily
    extreme at whole degrees, post-QC (NOT intraday METAR, NOT the NWS CLI
    summary, NOT the WU "Day High & Low" box). Stated precision rule is
    truncation for °C buckets (23.9°C → 23).
  - METAR has NO native °F anywhere (global °C, incl. US ASOS). US ASOS
    displays whole °F via rounding — our °C→°F round matches that display
    convention; the Polymarket truncation rule applies to the °C-bucket side
    where whole-degree METAR already aligns naturally.
- **F-market break-confirmation margin** (`reversal_strategy.py`,
  `break_confirm_margin_f` default 1.0, config key added): a °F-market fire
  requires the whole-degree converted extreme to clear the broken-bucket
  boundary by ≥1°F. Motivation: SF 9/4 low misfire — METAR 14°C converted to
  57.92°F < 58 (break), but Wunderground finalized 58.x°F (no break): METAR
  whole-°C granularity spans ±0.9°F after conversion and the finalized daily
  extreme can differ ~1°F from the intraday METAR extreme.
- **Back-test on real °F fills (2026-09-05→07, 5 trades)**: margin=1 would
  have kept SF 9/6 (YES@0.52 WON) and SF 9/7 (open), and filtered chicago
  9/5 low (NO@0.97 LOST — the SF-class false break) — but it would also have
  filtered atlanta 9/6 low (NO@0.92 WON + YES WON) and austin 9/5 high
  (YES@0.98 WON), both genuine near-boundary breaks. Trade-off is documented
  and tunable: 0.0 = legacy float behavior (fire all near-boundary breaks),
  1.0 = filter all <1°F-deep breaks (default, prevents SF-class false
  breaks at the cost of genuine near-boundary fills). C markets are exempt
  (whole-degree truncation aligns exactly).

## 2026-09-04 — Fire deadlock fix; WS live feed; paper-ledger fix (audited)

- **obs sanity window (was: absolute 180 s age gate → structurally zero fires).**
  METAR/SPECI obs_time age swings 0-60 min on hourly cadence (US AWS publish
  ~7 min early); `require_fresh_obs_seconds=180` made `stale_obs` block every
  fire. Replaced with sanity window `max_obs_lookback_seconds=5400` /
  `max_obs_future_seconds=900`: any NEW observation (deduped by
  `is_new_obs_time`) may fire unless the feed is >90 min behind or the stamp
  is >15 min in the future. First live fire within 27 min of deploy.
- **Full skip audit.** `_r_cycle` no longer silently drops skips: every
  re_skip / re_skip_yes / re_disarm is logged with reason/jump/consensus
  (silent skips previously hid the 0-fire deadlock).
- **NO cap 0.65 → 0.85** (broken-bucket NO redeems ~1.0; wider cap = fills);
  YES leg cap unchanged 0.48.
- **Universe: 10 → all 49 cities** (drop `active_icaos` allowlist; both high
  & low directions). `idle_metar_interval_seconds` 45 → 60 (49 cities = 3
  CheckWX batches; 4320 req/day < 5000 paid cap).
- **Market WebSocket live** (`market_ws_transport.py` stdlib-only WS client
  through the CONNECT proxy + `ws_bridge.py` daemon thread). 2000+ tokens
  subscribed; fresh (<5 s) WS LocalOrderBook snapshots overlay the ladder
  cache (epoch-guarded, never clobbers newer REST data); auto-reconnect
  5/10/30 s; REST /books remains the correctness backbone (the public market
  channel is near-frozen per py-clob-client #292 — WS is an accelerator).
- **Paper ledger fix.** `release()` no longer clamps total debit to zero —
  a negative debit is realized profit (equity = initial − debit). The clamp
  had silently discarded +52.80 USDC of paper profit (cost 49.06 vs payout
  101.86). `total_debit_usdc()` now reads negative values directly instead of
  through the `parsed >= 0` filter.
- **Audit hardening (pi + omp cross-review 2026-09-04):** `ensure_tokens`
  and `mark_disconnected` thread-safety (dict-size-change race during
  reconnect); `_ws_pump` epoch comparison made real (docstring now honest).
- Verified: 7/7 scenario tests; equity 1000 → 1052.79 after first US market
  settlements (NO legs 4/4 wins; one YES lottery leg lost).

## 2026-09-03 — Dual-rate paper runner; no σ; real-API soak

- **Zero σ / bias / fade-NO / dead-NO / BUY-YES** on the run path.
- Dual-rate METAR/books (ARM ~8s; idle METAR ~45s + consensus books ~30s).
- Dual-source METAR (CheckWX + AWC); C/F via `c_to_market_unit`; Gamma rules cache 20min.
- Modules: `runner_impl.py`, `_r_globals.py`, `_r_state.py`, `_r_data.py`, `_r_cycle.py`, `_r_exec.py`.
- **10-min paper soak (real CheckWX+Gamma+CLOB):** 49/49 METAR, 98 rules, Atlanta+Denver ARMed, 0 FIRE, capital 1000 USDC, no cycle_error.

## 2026-09-02 — Merge poly-yes2 paper infra; drop σ

- Not merged: TAF/σ arms. This repo is strategy + paper runtime.
