# -*- coding: utf-8 -*-
"""
Gate.io LTC_USDT 永续合约高频双向做市系统配置
"""
import os
import sys

def _load_env_file():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception as e:
            print(f"[WARN] 读取 .env 失败: {e}")

_load_env_file()


def _require_env(key: str, default: str = "") -> str:
    val = os.getenv(key, default)
    if not val:
        sys.exit(
            f"[FATAL] 缺少必需的环境变量: {key}\n"
            f"请在 .env 中配置: {key}=\"your_value\"，或执行 export {key}=\"your_value\" 后再启动服务。\n"
            f"切勿把真实密钥直接提交到 git 仓库。"
        )
    return val


CONFIG = {
    # API 凭据（优先读取环境变量及本地 .env 文件）
    "api_key": _require_env("GATE_API_KEY"),
    "api_secret": _require_env("GATE_API_SECRET"),

    # Web 控制面板鉴权 Token（用于保护 /api/start /api/stop 等控制接口，留空则不校验）
    "web_api_token": os.getenv("WEB_API_TOKEN", ""),

    # 模拟盘 REST API 域名（实盘为 https://api.gateio.ws）
    "rest_host": os.getenv("GATE_REST_HOST", "https://api-testnet.gateapi.io"),
    
    # 真实盘口 WebSocket 域名（用于获取毫秒级真实盘口流动性与微观价格）
    "real_ws_url": os.getenv("GATE_WS_URL", "wss://fx-ws.gateio.ws/v4/ws/usdt"),
    
    # 标的与合约规则
    "settle": "usdt",
    "contract": "LTC_USDT",
    "multiplier": 0.1,         # 1张 = 0.1 LTC
    "tick_size": 0.01,         # 最小跳价 0.01 USDT
    
    # 做市基础参数
    "target_spread_ticks": 2,  # 基础双向挂单价差 (2 ticks = 0.02 USDT)
    "order_size_contracts": 10, # 单边挂单张数（10张 = 1.0 LTC ≈ 54 USDT）
    "max_inventory_contracts": 30, # 单边最大库存上限（30张 = 3 LTC）
    "max_net_inventory": 20,       # 净敞口最大上限（|多仓 - 空仓| <= 20张，严格对冲风控）
    
    # ==================== 高级做市商 (MM) 量化模型参数 ====================
    # 1. Avellaneda-Stoikov (AS) 动态库存偏斜 (Inventory Skewing)
    "enable_inventory_skew": True,
    "inventory_skew_ticks_per_contract": 0.1, # 净多/空头每偏离1张合约产生的价差偏斜步长 (ticks)

    # 2. 订单簿微观失衡度 (OBI) 与 Stoikov Micro-Price
    "enable_micro_price": True,
    "obi_weight": 0.5, # 盘口失衡修正因子权重 (0.0~1.0)

    # 3. 自适应动态波动率价差 (Dynamic Spread)
    "enable_dynamic_spread": True,
    "max_spread_ticks": 6,     # 剧烈波动时最大价差保护阈值

    # 费率与返利模型（用于精确计算净利润与磨损）
    "maker_fee_rate": 0.00025, # 挂单万分之2.5
    "taker_fee_rate": 0.00050, # 吃单万分之5.0
    "rebate_rate": 0.75,       # 手续费返还 75%
    
    # 目标任务追踪
    "target_volume_usdt": 50_000_000.0, # 5000万 USDT 目标流水
    
    # 风控开关
    "enable_post_only": True,  # 开启 Post-Only (tif='poc')，确保 100% 享受 Maker 费率及返佣
    "auto_rebalance_timeout_sec": 30, # 超过 30 秒单边未平仓时，微调价格保本平水
    
    # Web 监控服务端口
    "web_host": "0.0.0.0",
    "web_port": 8888,
}
