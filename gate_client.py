# -*- coding: utf-8 -*-
"""
Gate.io API v4 异步客户端 (针对 USDT 永续合约)
"""
import time
import json
import hmac
import hashlib
import asyncio
import logging
from typing import Dict, Any, List, Optional
import aiohttp

logger = logging.getLogger("GateClient")

class GateFuturesClient:
    def __init__(self, api_key: str, api_secret: str, host: str = "https://api-testnet.gateapi.io", settle: str = "usdt"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.host = host.rstrip('/')
        self.settle = settle
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        return self.session

    def _sign(self, method: str, path: str, query: str = "", body: str = "") -> Dict[str, str]:
        t = str(int(time.time()))
        hashed_body = hashlib.sha512(body.encode('utf-8')).hexdigest()
        payload = f"{method}\n{path}\n{query}\n{hashed_body}\n{t}"
        sign = hmac.new(self.api_secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha512).hexdigest()
        return {
            "KEY": self.api_key,
            "Timestamp": t,
            "SIGN": sign,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    async def request(self, method: str, path: str, query: str = "", body: Any = None) -> Any:
        session = await self._get_session()
        body_str = json.dumps(body) if body is not None else ""
        headers = self._sign(method, path, query, body_str)
        url = f"{self.host}{path}"
        if query:
            url = f"{url}?{query}"

        try:
            async with session.request(method, url, data=body_str if body_str else None, headers=headers) as resp:
                resp_text = await resp.text()
                if resp.status in (200, 201):
                    try:
                        return json.loads(resp_text)
                    except Exception:
                        return resp_text
                else:
                    if method == "DELETE" and "ORDER_NOT_FOUND" in resp_text:
                        logger.debug(f"Gate API DELETE {path}: order already finished/cancelled")
                    else:
                        logger.warning(f"Gate API {method} {path} failed: {resp.status} - {resp_text}")
                    try:
                        parsed = json.loads(resp_text)
                        if isinstance(parsed, dict):
                            parsed["error"] = True
                            parsed["status"] = resp.status
                            return parsed
                    except Exception:
                        pass
                    return {"error": True, "status": resp.status, "message": resp_text}
        except Exception as e:
            logger.error(f"Network error on {method} {url}: {e}")
            return {"error": True, "message": str(e)}

    # --- 账户与资金 ---
    async def get_account(self) -> Dict[str, Any]:
        """查询 USDT 账户资产与可用保证金"""
        path = f"/api/v4/futures/{self.settle}/accounts"
        return await self.request("GET", path)

    # --- 仓位查询 ---
    async def get_position(self, contract: str = "LTC_USDT") -> Dict[str, Any]:
        """查询指定合约持仓"""
        path = f"/api/v4/futures/{self.settle}/positions/{contract}"
        return await self.request("GET", path)

    # --- 订单簿查询 ---
    async def get_orderbook(self, contract: str = "LTC_USDT", limit: int = 5) -> Dict[str, Any]:
        """获取盘口深度"""
        path = f"/api/v4/futures/{self.settle}/order_book"
        query = f"contract={contract}&limit={limit}"
        return await self.request("GET", path, query=query)

    # --- 挂单操作 ---
    async def place_order(self, contract: str, size: int, price: float, tif: str = "poc", text: str = "", reduce_only: bool = False) -> Dict[str, Any]:
        """
        下单：
        size > 0 为买入开多/买入平空
        size < 0 为卖出开空/卖出平多
        tif='poc' 为 Post-Only (只做 Maker)
        reduce_only=True 用于平仓，双向持仓模式下必须设置
        """
        path = f"/api/v4/futures/{self.settle}/orders"
        body = {
            "contract": contract,
            "size": size,
            "price": f"{price:.2f}",
            "tif": tif,
        }
        if text:
            body["text"] = text
        if reduce_only:
            body["reduce_only"] = True
        return await self.request("POST", path, body=body)

    # --- 撤单 ---
    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """取消单个订单"""
        path = f"/api/v4/futures/{self.settle}/orders/{order_id}"
        return await self.request("DELETE", path)

    async def cancel_all_orders(self, contract: str = "LTC_USDT") -> List[Any]:
        """撤销指定合约所有未完成订单"""
        path = f"/api/v4/futures/{self.settle}/orders"
        query = f"contract={contract}"
        return await self.request("DELETE", path, query=query)

    # --- 当前活动订单列表 ---
    async def get_open_orders(self, contract: str = "LTC_USDT") -> List[Dict[str, Any]]:
        """获取当前活跃订单"""
        path = f"/api/v4/futures/{self.settle}/orders"
        query = f"contract={contract}&status=open"
        res = await self.request("GET", path, query=query)
        if isinstance(res, list):
            return res
        return []

    # --- 成交历史 ---
    async def get_my_trades(self, contract: str = "LTC_USDT", limit: int = 50) -> List[Dict[str, Any]]:
        """获取最近个人成交记录"""
        path = f"/api/v4/futures/{self.settle}/my_trades"
        query = f"contract={contract}&limit={limit}"
        res = await self.request("GET", path, query=query)
        if isinstance(res, list):
            return res
        return []

    # --- 资金费流水 ---
    async def get_funding_history(self, contract: str = "LTC_USDT", limit: int = 50) -> List[Dict[str, Any]]:
        """查询账户账单中 type=fund（资金费结算）的记录"""
        path = f"/api/v4/futures/{self.settle}/account_book"
        query = f"contract={contract}&limit={limit}&type=fund"
        res = await self.request("GET", path, query=query)
        if isinstance(res, list):
            return res
        return []

    # --- 杠杆与保证金设置 (支持全仓/逐仓与双向持仓) ---
    async def set_leverage(self, contract: str, leverage: int, is_cross: bool = True) -> Dict[str, Any]:
        """
        显式设置合约杠杆倍数。
        全仓模式下 Gate.io 要求传 leverage=0 并指定 cross_leverage_limit。
        双向持仓路由为 dual_comp/positions。
        """
        path = f"/api/v4/futures/{self.settle}/dual_comp/positions/{contract}/leverage"
        if is_cross:
            query = f"leverage=0&cross_leverage_limit={leverage}"
        else:
            query = f"leverage={leverage}"
        res = await self.request("POST", path, query=query)
        # 如果 dual_comp 接口异常，尝试单向标准接口作为 fallback
        if isinstance(res, dict) and res.get("error"):
            single_path = f"/api/v4/futures/{self.settle}/positions/{contract}/leverage"
            res = await self.request("POST", single_path, query=query)
        return res

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
