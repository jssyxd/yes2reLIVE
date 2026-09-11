# 自动化运维与自愈指南 (Monitoring & Auto-Healing Runbook)

为了保证实盘程序 7x24 小时无人值守高可用运行，系统构建了全套健康检测、链上资金对账与故障自愈守护流程。

---

## 1. 监控体系设计 (Monitoring Architecture)

系统由以下两级监控保障：

1. **内建心跳 (`yes2re_health.json`)**：
   - 主循环每轮（5~10 秒）原子更新一次 `data/yes2re_health.json`。
   - 记录：当前运行模式、武装中的城市列表、WebSocket 接收消息总数、连接错误数、最新气象观测时效、订单簿缓存数。
2. **外部独立守护脚本 (`scripts/monitor_live.py`)**：
   - 由定时任务或外部看门狗每 30 分钟触发一次。
   - 通过系统独立进程对主服务进行无死角探测：
     - 检查 `systemctl is-active yes2re-live`。
     - 检查 `yes2re_health.json` 文件的最后修改时间戳（mtime）。如果心跳文件延迟超过 **120 秒**，判定为网络阻塞或内部死锁。
     - 调取 `live/reconcile.py`，调用 Polymarket 链上智能合约和 CLOB API 验证当前账户 USDC 真实余额、活跃挂单数与授权状态。
     - 若触发异常，立即执行自愈命令：`systemctl restart yes2re-live`，并在 3 秒内恢复服务。

---

## 2. 核心巡检脚本使用说明

### 2.1 执行即时健康诊断
```bash
# 激活虚拟环境后运行
.venv/bin/python scripts/monitor_live.py
```
**健康输出示例**：
```json
{
  "timestamp_utc": "2026-09-11T15:00:13.400604+00:00",
  "healthy": true,
  "actions_taken": [],
  "service": {
    "active": true,
    "status": "active"
  },
  "health_file": {
    "ok": true,
    "mode": "live",
    "age_s": 2.5,
    "stale": false,
    "armed_count": 21,
    "ws_connected": true,
    "ws_msgs": 6245575,
    "ws_errors": 0,
    "capital_initial": "51.713622",
    "remaining_capital": "51.713622"
  },
  "reconcile": {
    "ok": true,
    "usdc_balance": "51.713622",
    "open_orders_count": 0,
    "positions_count": 0
  }
}
```

### 2.2 运行事件全量统计分析
```bash
python3 scripts/analyze_events.py
```
此工具可全景扫描 `data/yes2re_events.jsonl`：
- 统计所有开火（`fire`）、武装（`arm`）、跳过（`skip`）原因分布。
- 逐一打印被共识过滤（`consensus_filter`）拦截的候选突破。
- 打印每笔真实开火的 3 级 FAK 阶梯报价、盘口 Ask 深度与成交状态。

---

## 3. 日常巡检故障排查指南 (Troubleshooting)

| 异常现象 | 可能原因 | 解决办法 |
| :--- | :--- | :--- |
| `health file stale (age > 120s)` | 外部 API 或网络套接字阻塞 | 自动触发 `systemctl restart yes2re-live` 即可在 3 秒内恢复。 |
| `reconcile failed: No module named 'py_clob_client'` | 运行环境缺少实盘 SDK | 确保通过 `.venv/bin/python` 执行脚本。 |
| `ws_connected: false` | 节点与 Polymarket WebSocket 断连 | 系统自带指数退避自动重连机制（1s, 2s, 4s），通常 5 秒内自动恢复。 |
| `fire_insufficient_capital` | 可用 USDC 余额低于单笔开火预算 | 充值 Polygon 链上 USDC 至交易代理钱包或调整 `budget_usdc` 参数。 |
