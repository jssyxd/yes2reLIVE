# 实盘部署运维手册 (Production Deployment Runbook)

本文档面向 Linux 服务器（Debian 12 / Ubuntu 22.04 LTS）环境，提供从系统初始化、依赖配置、服务守护、自愈定时器到日常运维的全流程指南。

---

## 1. 系统准备与优化 (Server Provisioning)

### 1.1 虚拟内存优化 (Swap Configuration)
在轻量 VPS（如 1 vCPU / 1GB RAM）上，缓存多达 2,100+ 个 Token 的实时 L2 深度订单簿需要足够的内存余量。强烈建议配置 1GB Swap：
```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo sysctl vm.swappiness=10
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
```

### 1.2 系统基础工具安装
```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip curl htop jq
```

---

## 2. 代码部署与虚拟环境搭建

```bash
# 1. 克隆代码至推荐目录
git clone https://github.com/jssyxd/yes2reLIVE.git /root/weatherbotyes2re
cd /root/weatherbotyes2re

# 2. 搭建 Python 虚拟环境 (隔离 py-clob-client-v2 与系统 Python)
python3 -m venv /root/yes2re-live/.venv
/root/yes2re-live/.venv/bin/pip install --upgrade pip
/root/yes2re-live/.venv/bin/pip install py-clob-client

# 3. 环境变量配置
cp .env.example .env
nano .env
```

---

## 3. 环境变量与安全配置 (`.env`)

编辑 `.env` 文件，确保包含以下必要参数：
```ini
# ==========================================
# 1. 交易模式与执行开关 (必须开启实盘门禁)
# ==========================================
YES2RE_MODE=live
YES2RE_LIVE_ENABLE_SUBMIT=1
LIVE_SUBMIT_ENABLED=1

# ==========================================
# 2. Polymarket 账户与凭证 (绝不可泄露)
# ==========================================
POLYMARKET_HOST=https://clob.polymarket.com
CHAIN_ID=137
WALLET_ADDRESS=0xYourWalletAddress...
PRIVATE_KEY=0xYourPrivateKey...

# ==========================================
# 3. CLOB API Key 凭证 (由 Polymarket 生成)
# ==========================================
CLOB_API_KEY=your_clob_api_key
CLOB_API_SECRET=your_clob_secret
CLOB_API_PASSPHRASE=your_clob_passphrase

# ==========================================
# 4. 气象数据接口 (CheckWX API Key 可选，AWC 官方源免费直接提供)
# ==========================================
CHECKWX_API_KEY=your_checkwx_key_here
```

---

## 4. Systemd 生产守护与自动滚动服务

系统采用主守护进程 + 每日 UTC 定时滚动架构：

### 4.1 主服务单元：`yes2re-live.service`
文件路径：`/etc/systemd/system/yes2re-live.service`
```ini
[Unit]
Description=weatherbotyes2re LIVE runner (real CLOB v2 orders)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/weatherbotyes2re
EnvironmentFile=-/root/weatherbotyes2re/.env
ExecStart=/root/weatherbotyes2re/run_live.sh
Restart=always
RestartSec=5
KillMode=process
TimeoutStopSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 4.2 启动脚本：`run_live.sh`
文件路径：`/root/weatherbotyes2re/run_live.sh`
```bash
#!/bin/bash
set -e
cd /root/weatherbotyes2re
set -a
. ./.env
export YES2RE_LIVE_ENABLE_SUBMIT=1
export LIVE_SUBMIT_ENABLED=1
export YES2RE_LIVE_CONFIRM="SMOKE-$(date -u +%Y-%m-%d)"
set +a
exec /root/yes2re-live/.venv/bin/python reversal_runner.py run --config config/yes2re_reversal.json
```
记得赋予执行权限：`chmod +x /root/weatherbotyes2re/run_live.sh`

### 4.3 每日 UTC 00:01 自动滚动重载
为了确保跨天预测市场与时间戳平稳切换，配置每日定时器：
- `/etc/systemd/system/yes2re-live-daily.service`：
```ini
[Unit]
Description=Daily rollover restart for yes2re-live

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart yes2re-live.service
```
- `/etc/systemd/system/yes2re-live-daily.timer`：
```ini
[Unit]
Description=Restart yes2re-live daily at 00:01:00 UTC

[Timer]
OnCalendar=*-*-* 00:01:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

### 4.4 激活并启动所有服务
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now yes2re-live.service
sudo systemctl enable --now yes2re-live-daily.timer

# 查看状态
systemctl status yes2re-live.service
systemctl list-timers yes2re-live-daily.timer
```

---

## 5. 常见运维命令

```bash
# 查看实时运行日志
journalctl -u yes2re-live -f

# 执行自动化健康巡检与对账
/root/yes2re-live/.venv/bin/python scripts/monitor_live.py

# 分析当天的突破与开火事件
python3 scripts/analyze_events.py

# 查看订单簿实时状态
tail -n 30 data/yes2re_health.json | jq .
```
