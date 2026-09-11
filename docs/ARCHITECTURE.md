# 系统架构与模块设计 (Architecture & Module Design)

`yes2reLIVE` 是一个专为 Polymarket 天气预测市场设计的高稳定性、低延迟实盘量化交易系统。
系统底层框架基于事件循环与阻塞轮询结合的设计，兼顾高吞吐订单流（WebSocket）与高容错性（REST Fallback）。

---

## 1. 系统分层设计 (Five-Layer Architecture)

```
+---------------------------------------------------------------+
| Layer 1: Weather & Discovery (气象观测与市场规则自动发现)         |
|   research/common.py (METAR/TAF 解析, 双源仲裁)                 |
|   market_adapter.py  (Gamma REST 并行抓取, 98+ 市场自动建模)    |
+---------------------------------------------------------------+
                               |
                               v
+---------------------------------------------------------------+
| Layer 2: Market Data Engine (只读盘口深度行情引擎)              |
|   market_ws_transport.py (CLOB L2 WebSocket, 2100+ Tokens)    |
|   clob_market_data.py    (CLOB REST 100-chunk 批量降级)        |
|   adapters/polymarket/orderbook.py -> execution/market.BookView|
+---------------------------------------------------------------+
                               |
                               v
+---------------------------------------------------------------+
| Layer 3: Decision Engine (纯函数交易决策与共识过滤)             |
|   reversal_strategy.py (状态机: IDLE -> ARMED -> FIRED)       |
|   consensus_tracker.py (滚动 7200 样本 TWAP Mid 概率榜过滤)    |
+---------------------------------------------------------------+
                               |
                               v
+---------------------------------------------------------------+
| Layer 4: Execution Engine (3级限价 FAK 阶梯执行与撮合)         |
|   re_execution.py      (FAK Ladder: t=0ms, 1500ms, 4000ms)    |
|   live/v2_transport.py (EIP-712 签名, CLOB v2 订单提交)        |
|   paper_capital.py     (资金账本, 失败强制关闭 Fail-Closed)    |
+---------------------------------------------------------------+
                               |
                               v
+---------------------------------------------------------------+
| Layer 5: State, Audit & Operations (原子状态、事件审计与自愈)   |
|   _r_state.py          (原子写入 data/yes2re_state.json)       |
|   _r_cycle.py          (周期心跳 data/yes2re_health.json)      |
|   scripts/monitor_live.py (30分钟链上资金对账与自愈服务管理)    |
+---------------------------------------------------------------+
```

---

## 2. 关键数据流与对象归一化 (Data Flow & Normalization)

### 2.1 盘口视图归一化 (`execution/market.py`)
为了确保执行层与底层传输协议彻底解耦，系统通过 `adapters/polymarket/orderbook.py` 的 `from_any` 函数将来自 WebSocket 的增量深度或 REST 的全量快照统一转化为标准的 `BookView`：
```python
@dataclass(frozen=True)
class BookView:
    token_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    timestamp: datetime
    tick_size: Decimal
```
执行逻辑永远只针对 `BookView` 决策，绝不关心数据是通过 WebSocket 还是 REST 获取。

### 2.2 双源气象观测仲裁 (`research/common.py`)
系统并行查询两个全球航空气象源：
- **AviationWeather.gov (AWC)**：官方权威源，免费直连，包含完整历史逐时观测。
- **CheckWX API**：高频商业镜像，毫秒级响应。
两源交汇时，系统执行 `fresher obs wins` 原则（时间戳更新者优先），并对异常跳跃执行物理合理性校验（例如气温突变不得超过 15°C/h）。

### 2.3 状态机运转模型
每个交易标的拥有独立的会话键：`session_key = {city_id}|{market_local_date}|{direction}`。
- **IDLE**：日常观测，定期抓取行情与长周期共识。
- **ARMED**：当实测气温距离基准极值桶还有 1 个单位时，触发高速武装模式（Fast-poll 仅针对已武装的 ICAO 机场，每 10 秒刷新一次）。
- **FIRED**：气温突破发生，进入 8000ms 的 FAK 阶梯买入。一个 session_key 每天严格仅允许开火一次。
- **COOLDOWN / SETTLED**：开火完成后进入冷却，等待当地午夜交割。
