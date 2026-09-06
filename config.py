# -*- coding: utf-8 -*-
"""
Gate.io LTC_USDT 永续合约高频双向做市系统配置
"""
import os

CONFIG = {
    # API 凭据（模拟盘）
    "api_key": os.getenv("GATE_API_KEY", "5e7fcd4ac77886cca79fe6341c2d3f5f"),
    "api_secret": os.getenv("GATE_API_SECRET", "96bf8a20c79c57442164a7ba18f41f8a0dc17e178eb57374a329b13749916757"),
    
    # 模拟盘 REST API 域名
    "rest_host": "https://api-testnet.gateapi.io",
    
    # 真实盘口 WebSocket 域名（用于获取毫秒级真实盘口流动性与微观价格）
    "real_ws_url": "wss://fx-ws.gateio.ws/v4/ws/usdt",
    
    # 标的与合约规则
    "settle": "usdt",
    "contract": "LTC_USDT",
    "multiplier": 0.1,         # 1张 = 0.1 LTC
    "tick_size": 0.01,         # 最小跳价 0.01 USDT
    
    # 做市核心参数
    "target_spread_ticks": 2,  # 核心逻辑：双向挂单天然保留 2 个 ticks 利润
    "order_size_contracts": 10, # 单边挂单张数（10张 = 1.0 LTC ≈ 54 USDT）
    "max_inventory_contracts": 30, # 单边最大库存上限（30张 = 3 LTC）
    "max_net_inventory": 20,       # 净敞口最大上限（|多仓 - 空仓| <= 20张，严格对冲风控）
    
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
