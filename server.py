# -*- coding: utf-8 -*-
"""
Gate.io LTC 5000W 做市商系统 Web 监控服务端 (FastAPI - Modern Lifespan)
"""
import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from config import CONFIG
from gate_client import GateFuturesClient
from strategy import MarketMakerStrategy

# 初始化客户端与策略
client = GateFuturesClient(CONFIG["api_key"], CONFIG["api_secret"], CONFIG["rest_host"])
strategy = MarketMakerStrategy(client, CONFIG)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动阶段
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

@app.post("/api/start")
async def start_mm():
    await strategy.start()
    return JSONResponse({"status": "ok", "message": "Market making started"})

@app.post("/api/stop")
async def stop_mm():
    await strategy.stop()
    return JSONResponse({"status": "ok", "message": "Market making stopped"})

@app.post("/api/emergency_cancel")
async def emergency_cancel():
    await strategy.emergency_cancel()
    return JSONResponse({"status": "ok", "message": "All orders cancelled"})

@app.post("/api/reset_stats")
async def reset_stats():
    strategy.reset_stats()
    return JSONResponse({"status": "ok", "message": "Statistics reset"})

@app.post("/api/update_params")
async def update_params(params: ParamUpdate):
    CONFIG["order_size_contracts"] = params.order_size_contracts
    CONFIG["target_spread_ticks"] = params.target_spread_ticks
    CONFIG["max_inventory_contracts"] = params.max_inventory_contracts
    CONFIG["max_net_inventory"] = params.max_net_inventory
    strategy.config = CONFIG
    strategy.log("INFO", f"参数已更新: 单笔={params.order_size_contracts}张, 目标利差={params.target_spread_ticks} ticks, 单边上限={params.max_inventory_contracts}张, 净敞口上限={params.max_net_inventory}张")
    return JSONResponse({"status": "ok", "config": CONFIG})

if __name__ == "__main__":
    uvicorn.run(app, host=CONFIG["web_host"], port=CONFIG["web_port"], log_level="info")
