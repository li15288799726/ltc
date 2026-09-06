# Gate.io LTC_USDT 高频双向对冲做市系统 (Dual Mode Pro)

本项目是一套专为 Gate.io **LTC_USDT 永续合约** 设计的高频双向对冲做市商（Market Maker）系统，内置 Web 实时监控面板与风控引擎。

---

## 核心特性

1. **独立双向持仓对冲 (Delta Neutral)**
   - 多头仓位 (`dual_long`) 与 空头仓位 (`dual_short`) 独立核算与对冲。
   - 买方：挂【平空单】(止盈) + 【开多单】(做市建仓)。
   - 卖方：挂【平多单】(止盈) + 【开空单】(做市建仓)。
2. **净敞口风控约束**
   - 限制 `|多仓 - 空仓| <= max_net_inventory`（默认 20 张），对冲部分不占用风险额度。
   - 超时单边持仓自动保本平水再平衡，避免单边趋势单向套牢。
3. **100% Maker 费率模型**
   - 全程开启 Post-Only（`tif='poc'`），确保只做挂单流动性提供者，杜绝 Taker 磨损，最大化手续费返佣。
4. **实时低延迟架构**
   - 真实盘口毫秒级 WebSocket 流式订阅。
   - 异步并发对账与 REST 批量下单/撤单撤回。
5. **现代响应式 Web 监控仪表盘**
   - FastAPI 后端驱动，支持浏览器端实时查看盘口微观结构、双向持仓、成交流水、返佣收益统计。
   - 提供策略启停、单边张数/敞口参数热更新、一键紧急全撤单风控操作。

---

## 目录结构

```
gate_mm_bot/
├── config.py           # 系统核心配置（合约、API凭证、风控阈值、网格步长等）
├── gate_client.py      # Gate.io Futures REST API v4 异步签名与请求客户端
├── strategy.py         # 高频双向做市核心策略引擎
├── server.py           # FastAPI Web 监控终端服务端
├── static/
│   └── index.html      # 前端现代化监控仪表盘页面
├── start_server.sh     # 守护启动脚本（支持 systemd 或 nohup 后台脱机运行）
├── stop_server.sh      # 安全平仓/撤单退出脚本
├── requirements.txt    # Python 依赖清单
└── README.md           # 项目说明文档
```

---

## 快速上手

### 1. 环境依赖

建议使用 Python 3.10+ 环境：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置 API 凭证与参数

在 `config.py` 中配置环境变量或直接修改默认参数：

```bash
export GATE_API_KEY="your_api_key_here"
export GATE_API_SECRET="your_api_secret_here"
```

主要参数说明：
- `contract`: 交易标的（默认 `LTC_USDT`）
- `multiplier`: 合约面值（`1张 = 0.1 LTC`）
- `target_spread_ticks`: 买卖双向最小保留价差（Ticks）
- `order_size_contracts`: 单边挂单张数（默认 10 张）
- `max_inventory_contracts`: 单边最大仓位上限
- `max_net_inventory`: 净敞口最大上限（严格对冲风控）

### 3. 启动服务

通过脚本启动：
```bash
bash start_server.sh
```

或直接在前台启动调试：
```bash
python3 server.py
```

### 4. 访问监控终端

启动后，在浏览器访问：
```
http://<服务器IP>:8888
```

---

## 安全与风控

- **紧急撤单**：可在 Web 面板点击「紧急撤单」或运行 `bash stop_server.sh`，系统将自动撤销所有挂单并退出。
- **实盘上线提示**：默认配置连接模拟盘网关，实盘部署前请将 `config.py` 中的 `rest_host` 切换至 Gate.io 实盘 REST API 域名并配置正式 API Key。
