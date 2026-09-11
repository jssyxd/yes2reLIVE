# Weatherbotyes2re — 部署运行记录 (部署于 2026-09-05)

## 部署位置
- 项目根: 本目录 (`/home/da/桌面/old version/weatherbotyes2re-d2393e29.../`)
- 运行入口: `reversal_runner.py run --config config/yes2re_reversal.json`
- 模式: **paper** (reversal_runner 硬性拒绝非 paper 模式), 无真实下单
- 初始资金: **1000.0 USDC** (config `paper_initial_capital_usdc`)
- 火力预算: 单笔 fire ≤ 20 USDC
- 城市范围: `config/contract_cities.json` 全部 **49 城**, 且**高温(high)与低温(low)均参与**
  (规则发现为 49×2=98 条; 当前健康数据 rules=98, failures={})

## 数据源
- METAR: CheckWX(带 key, 在 `.env` / 环境变量 `CHECKWX_API_KEY`) + AviationWeather(AWC 无 key), 双源合并
- TAF TX/TN: CheckWX (可选; 缺省回退市场 rank-1 共识)
- 规则/市场: Polymarket Gamma 公开 REST (只读)
- 盘口: CLOB REST + 可选 Market WS, 均为只读 (paper)

## 运行组件 (全部由本部署启动)
| 组件 | 进程形态 | 说明 |
|---|---|---|
| paper runner | supervisor 守护 (`ops/yes2re_supervisor.py`) 拉起 `reversal_runner.py run`, 意外退出自动重启 | 主交易循环, 见 `data/yes2re_health.json` / `data/yes2re_state.json` |
| 15-min 记录器 | cron `*/15 * * * *` 调 `ops/yes2re_recorder.py` | 每 15 分钟把状态/成交/异常写入 CSV + 日志 |

## 输出文件 (data/)
- `yes2re_health.json` — 运行健康 (armed/fired/capital/rules/metar/books/ws)
- `yes2re_state.json` — 持仓/成交状态 (paper ledger)
- `yes2re_events.jsonl` — 事件流 (arm/fire/settle/error…)
- `yes2re_status.csv` — 每 15 分钟一行运行快照 (cron 记录器)
- `yes2re_run.log`    — 每 15 分钟运行情况 + BUG/TRADE 明细 (cron 记录器)
- `trades.csv`        — 交易记录 (fire/settle) 追加式 CSV
- `supervisor.log` / `runner_stdout.log` — 守护/运行日志

## 检查命令
```bash
python3 reversal_runner.py status --config config/yes2re_reversal.json
tail -20 data/yes2re_run.log
cat data/yes2re_status.csv
crontab -l | grep yes2re          # 15 分钟记录器
ps aux | grep -E "yes2re_supervisor|reversal_runner"
```

## 备注 (部署时发现并修复的问题)
- 本目录原 zip/快照是 d2393e2 中间态: `runner_impl.py` 引用的 `_r_state/_r_exec` 在树中缺失,
  无法直接运行 (该仓库 AGENTS.md 亦有说明). 已将树同步为审计后的可运行状态
  (与 `yes2re/weatherbotyes2re` HEAD c575d93 + 工作树修复一致)。
- 修复 `clob_market_data.py::fetch_books` 中 `parsed` 未初始化导致的
  `NameError: name 'parsed' is not defined` — 该 bug 曾使书盘永远拉不到
  (旧实例 1.5h 0 成交, health books cached=0)。修复后 books cached=2156。
- 停掉了上一阶段 (supervised daemon `yes2re-paper`/`yes2re-hourly`, 指向 yes2re 目录)
  以避免同一 CheckWX key 双实例超配额; 本目录部署为唯一实例。
