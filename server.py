# -*- coding: utf-8 -*-
"""
Gate.io LTC 5000W 做市商系统 Web 监控服务端 (FastAPI - Modern Lifespan)
"""
import os
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Request, Depends, HTTPException, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from config import CONFIG
from gate_client import GateFuturesClient
from strategy import MarketMakerStrategy

logger = logging.getLogger("Server")

# 初始化客户端与策略
client = GateFuturesClient(CONFIG["api_key"], CONFIG["api_secret"], CONFIG["rest_host"])
strategy = MarketMakerStrategy(client, CONFIG)


def require_api_token(x_api_token: str = Header(default=""), token: str = Query(default="")):
    """
    保护控制接口（启停策略、撤单、重置统计、改参数）。
    如果 WEB_API_TOKEN 为空，则视为本地/内网环境，免鉴权通行。
    如果配置了 WEB_API_TOKEN，则支持通过 X-API-Token Header 或 token Query 参数鉴权。
    """
    expected = CONFIG.get("web_api_token", "")
    if not expected:
        return
    req_token = x_api_token or token
    if req_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Token header")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动阶段
    if not CONFIG.get("web_api_token"):
        logger.warning(
            "⚠️  WEB_API_TOKEN 未设置！控制接口当前免鉴权。"
            "如部署在公网，建议在 .env 中设置 WEB_API_TOKEN。"
        )
    try:
        await strategy._update_market_and_account()
        strategy.log("INFO", "Web 服务端与 Gate 客户端就绪")
        await strategy.start()
    except Exception as e:
        strategy.log("WARN", f"初始化市场账户或自启动策略失败: {e}")
    yield
    # 关闭阶段
    await strategy.stop()
    await client.close()

app = FastAPI(title="Gate.io LTC MM Pro Terminal", lifespan=lifespan)

# 挂载静态目录
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

class ParamUpdate(BaseModel):
    order_size_contracts: int = 10
    target_spread_ticks: int = 2
    max_inventory_contracts: int = 30
    max_net_inventory: int = 20
    enable_inventory_skew: Optional[bool] = None
    inventory_skew_ticks_per_contract: Optional[float] = None
    enable_micro_price: Optional[bool] = None

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h2>Terminal Loading...</h2>")

@app.get("/api/status")
async def get_status():
    if not strategy.is_running:
        try:
            await strategy._update_market_and_account()
        except Exception:
            pass
    return JSONResponse(strategy.get_full_status())

@app.post("/api/start", dependencies=[Depends(require_api_token)])
async def start_mm():
    await strategy.start()
    return JSONResponse({"status": "ok", "message": "Market making started"})

@app.post("/api/stop", dependencies=[Depends(require_api_token)])
async def stop_mm():
    await strategy.stop()
    return JSONResponse({"status": "ok", "message": "Market making stopped"})

@app.post("/api/emergency_cancel", dependencies=[Depends(require_api_token)])
async def emergency_cancel():
    await strategy.emergency_cancel()
    return JSONResponse({"status": "ok", "message": "All orders cancelled"})

@app.post("/api/reset_stats", dependencies=[Depends(require_api_token)])
async def reset_stats():
    strategy.reset_stats()
    return JSONResponse({"status": "ok", "message": "Statistics reset"})

@app.post("/api/update_params", dependencies=[Depends(require_api_token)])
async def update_params(params: ParamUpdate):
    CONFIG["order_size_contracts"] = params.order_size_contracts
    CONFIG["target_spread_ticks"] = params.target_spread_ticks
    CONFIG["max_inventory_contracts"] = params.max_inventory_contracts
    CONFIG["max_net_inventory"] = params.max_net_inventory
    if params.enable_inventory_skew is not None:
        CONFIG["enable_inventory_skew"] = params.enable_inventory_skew
    if params.inventory_skew_ticks_per_contract is not None:
        CONFIG["inventory_skew_ticks_per_contract"] = params.inventory_skew_ticks_per_contract
    if params.enable_micro_price is not None:
        CONFIG["enable_micro_price"] = params.enable_micro_price

    strategy.config = CONFIG
    strategy.log("INFO", f"参数已更新: 单笔={params.order_size_contracts}张, 目标利差={params.target_spread_ticks} ticks, 单边上限={params.max_inventory_contracts}张, 净敞口上限={params.max_net_inventory}张, AS偏斜={CONFIG.get('enable_inventory_skew')}, MicroPrice={CONFIG.get('enable_micro_price')}")
    return JSONResponse({"status": "ok", "config": CONFIG})

if __name__ == "__main__":
    uvicorn.run(app, host=CONFIG["web_host"], port=CONFIG["web_port"], log_level="info")
