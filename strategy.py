# -*- coding: utf-8 -*-
"""
Gate.io LTC_USDT 永续合约高频双向对冲做市系统 (V4 独立多空双仓对冲引擎)
核心机制：
1. 真正双向持仓：多头仓位 (dual_long) 与 空头仓位 (dual_short) 同时常驻对冲 (Delta Neutral)
2. 独立双边做市循环：
   - 买方：挂【平空单】(reduce_only=True) 止盈 + 挂【开多单】(reduce_only=False) 做市建仓
   - 卖方：挂【平多单】(reduce_only=True) 止盈 + 挂【开空单】(reduce_only=False) 做市建仓
3. 净敞口严格风控：|多仓 - 空仓| <= max_net_inventory (默认 20 张)，多空对冲部分不占用风险额度
4. 毫秒级 WebSocket 盘口推流与并发异步账户对账
"""
import asyncio
import time
import logging
import json
from typing import Dict, Any, List, Optional
from datetime import datetime
import aiohttp

logger = logging.getLogger("MMStrategy")

class MarketMakerStrategy:
    def __init__(self, client, config: Dict[str, Any]):
        self.client = client
        self.config = config
        
        # 运行状态
        self.is_running = False
        self._main_task: Optional[asyncio.Task] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        
        # 统计指标
        self.stats = {
            "total_volume_usdt": 0.0,
            "target_volume_usdt": self.config.get("target_volume_usdt", 50_000_000.0),
            "progress_pct": 0.0,
            "total_trades": 0,
            "maker_trades": 0,
            "maker_ratio": 100.0,
            "long_trades": 0,            # 多头腿成交计数 (开多/平多)
            "short_trades": 0,           # 空头腿成交计数 (开空/平空)
            "nominal_fees_usdt": 0.0,
            "rebate_usdt": 0.0,
            "gross_profit_usdt": 0.0,
            "net_pnl_usdt": 0.0,
            "running_time_seconds": 0,
            "start_timestamp": None,
            "initial_equity_usdt": 497.32,
        }
        
        # 市场深度快照
        self.market_data = {
            "last_price": 0.0,
            "best_bid": 0.0,
            "best_ask": 0.0,
            "spread_ticks": 2,
            "spread_usdt": 0.02,
            "mid_price": 0.0,
            "orderbook_update_time": 0,
            "data_source": "INITIALIZING",
        }
        
        # 账户与多空双仓数据
        self.account_data = {
            "total_usdt": 497.32,
            "available_usdt": 496.08,
            "long_size": 10,             # 多头持仓张数 (dual_long)
            "long_ltc": 1.0,
            "short_size": 10,            # 空头持仓张数 (dual_short)
            "short_ltc": 1.0,
            "net_position": 0,           # 净敞口 (long_size - short_size)
            "net_ltc": 0.0,
            "long_entry_price": 0.0,
            "short_entry_price": 0.0,
            "long_unreal_pnl": 0.0,
            "short_unreal_pnl": 0.0,
            "total_unrealised_pnl": 0.0,
            "position_mode": "dual",
            "leverage": 10,
        }
        
        # 活动挂单记录 (4 条通道)
        self.active_quotes = {
            "t-mm-closelong": None,      # 卖一平多单
            "t-mm-openshort": None,      # 卖一开空单
            "t-mm-closeshort": None,     # 买一平空单
            "t-mm-openlong": None,       # 买一开多单
            # 兼容老仪表盘
            "bid": None,
            "ask": None,
        }
        
        self.recent_trades: List[Dict[str, Any]] = []
        self.audit_logs: List[Dict[str, Any]] = []
        self.known_trade_ids = set()
        
        # 离散开仓价位追踪：严格保证“一个价位只开一次仓位”
        self.active_long_prices: List[float] = []
        self.active_short_prices: List[float] = []
        
        # 活跃持仓计时器（用于超时平水微调）
        self.last_long_trade_time: float = time.time()
        self.last_short_trade_time: float = time.time()

    def log(self, level: str, msg: str):
        now_str = datetime.now().strftime("%H:%M:%S")
        entry = {"time": now_str, "level": level.upper(), "msg": msg}
        self.audit_logs.insert(0, entry)
        if len(self.audit_logs) > 80:
            self.audit_logs.pop()
        logger.info(f"[{level.upper()}] {msg}")


    async def _update_market_and_account(self):
        """单次刷新市场盘口与账户持仓"""
        try:
            acc_res, pos_res, ob_res = await asyncio.gather(
                self.client.get_account(),
                self.client.get_position(self.config['contract']),
                self.client.get_orderbook(self.config['contract'], limit=5),
                return_exceptions=True
            )
            if isinstance(acc_res, dict) and not acc_res.get('error'):
                self.account_data['total_usdt'] = float(acc_res.get('total', 0.0))
                self.account_data['available_usdt'] = float(acc_res.get('available', 0.0))

            long_size = 0
            short_size = 0
            long_entry = 0.0
            short_entry = 0.0
            long_u = 0.0
            short_u = 0.0
            if isinstance(pos_res, list):
                for p in pos_res:
                    m = p.get('mode')
                    sz = abs(int(p.get('size', 0)))
                    if m == 'dual_long' and sz > 0:
                        long_size = sz
                        long_entry = float(p.get('entry_price', 0.0))
                        long_u = float(p.get('unrealised_pnl', 0.0))
                    elif m == 'dual_short' and sz > 0:
                        short_size = sz
                        short_entry = float(p.get('entry_price', 0.0))
                        short_u = float(p.get('unrealised_pnl', 0.0))

            self.account_data.update({
                'long_size': long_size,
                'long_ltc': round(long_size * self.config['multiplier'], 2),
                'short_size': short_size,
                'short_ltc': round(short_size * self.config['multiplier'], 2),
                'net_position': long_size - short_size,
                'net_ltc': round((long_size - short_size) * self.config['multiplier'], 2),
                'long_entry_price': long_entry,
                'short_entry_price': short_entry,
                'long_unreal_pnl': round(long_u, 4),
                'short_unreal_pnl': round(short_u, 4),
                'total_unrealised_pnl': round(long_u + short_u, 4),
            })

            if isinstance(ob_res, dict) and 'bids' in ob_res and 'asks' in ob_res:
                bids = ob_res.get('bids', [])
                asks = ob_res.get('asks', [])
                if bids and asks:
                    best_bid = float(bids[0]['p'])
                    best_ask = float(asks[0]['p'])
                    spread = max(0.01, best_ask - best_bid)
                    mid_p = (best_bid + best_ask) / 2.0
                    self.market_data.update({
                        'best_bid': best_bid,
                        'best_ask': best_ask,
                        'spread_usdt': round(spread, 4),
                        'spread_ticks': max(1, int(round(spread / self.config['tick_size']))),
                        'mid_price': round(mid_p, 4),
                        'last_price': round(mid_p, 2),
                        'orderbook_update_time': time.time(),
                        'data_source': 'REST_INIT'
                    })
        except Exception as e:
            logger.error(f'_update_market_and_account error: {e}')

    def reset_stats(self):
        """重置初始化本次测试的统计数据"""
        now = time.time()
        curr_total = self.account_data.get("total_usdt", 497.32)
        self.stats.update({
            "total_volume_usdt": 0.0,
            "progress_pct": 0.0,
            "total_trades": 0,
            "maker_trades": 0,
            "maker_ratio": 100.0,
            "long_trades": 0,
            "short_trades": 0,
            "nominal_fees_usdt": 0.0,
            "rebate_usdt": 0.0,
            "gross_profit_usdt": 0.0,
            "net_pnl_usdt": 0.0,
            "running_time_seconds": 0,
            "start_timestamp": now,
            "initial_equity_usdt": curr_total,
        })
        self.recent_trades.clear()
        self.known_trade_ids.clear()
        self.log("INFO", f"🧹 统计数据已重置归零 (基准权益: {curr_total:.2f} USDT)")

    async def _ensure_base_inventory(self):
        """开局检查底仓，确保多空双向各有基础底仓 (如各 10 张) 实现真正 Delta Neutral"""
        base_size = self.config.get("order_size_contracts", 10)
        contract = self.config["contract"]
        try:
            pos_res = await self.client.get_position(contract)
            long_size = 0
            short_size = 0
            if isinstance(pos_res, list):
                for p in pos_res:
                    m = p.get("mode")
                    sz = abs(int(p.get("size", 0)))
                    if m == "dual_long":
                        long_size = sz
                    elif m == "dual_short":
                        short_size = sz
            
            diff_long = base_size - long_size
            diff_short = base_size - short_size
            
            if diff_long > 0 or diff_short > 0:
                ob = await self.client.get_orderbook(contract, limit=5)
                if isinstance(ob, dict) and "bids" in ob and "asks" in ob:
                    bids = ob.get("bids", [])
                    asks = ob.get("asks", [])
                    if bids and asks:
                        best_bid = float(bids[0]["p"])
                        best_ask = float(asks[0]["p"])
                        tick_size = self.config.get("tick_size", 0.01)
                        if diff_long > 0:
                            p_bid = round(min(best_bid, best_ask - tick_size), 2)
                            self.log("INFO", f"📦 初始化多头底仓: 挂单开多 {diff_long} 张 @ {p_bid} (Maker POC)...")
                            await self.client.place_order(contract, diff_long, p_bid, tif="poc", text="t-base-long", reduce_only=False)
                        if diff_short > 0:
                            p_ask = round(max(best_ask, best_bid + tick_size), 2)
                            self.log("INFO", f"📦 初始化空头底仓: 挂单开空 {diff_short} 张 @ {p_ask} (Maker POC)...")
                            await self.client.place_order(contract, -diff_short, p_ask, tif="poc", text="t-base-short", reduce_only=False)
        except Exception as e:
            self.log("WARN", f"底仓检查异常: {e}")

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        if not self.stats["start_timestamp"]:
            self.stats["start_timestamp"] = time.time()
        
        base_size = self.config.get("order_size_contracts", 10)
        net_max = self.config.get("max_net_inventory", 20)
        self.log("INFO", f"🚀 高频双向对冲做市系统 V4 启动 (底仓基准: {base_size}张多+{base_size}张空, 净敞口限额: {net_max}张)")
        
        # 1. 启动前安全清理所有遗留历史残留挂单
        try:
            await self.client.cancel_all_orders(self.config["contract"])
            self.active_quotes = {k: None for k in self.active_quotes}
            self.log("INFO", "🧹 启动前已撤销全部遗留历史挂单，恢复干净盘口")
        except Exception as e:
            self.log("WARN", f"清理遗留挂单异常: {e}")

        # 2. 单次同步最新盘口与账户
        await self._update_market_and_account()

        # 3. 确保双向底仓齐备
        await self._ensure_base_inventory()
        
        # 4. 启动 WebSocket 毫秒级盘口监听
        self._ws_task = asyncio.create_task(self._ws_orderbook_loop())
        # 5. 启动账户与双仓状态后台同步
        self._sync_task = asyncio.create_task(self._account_sync_loop())
        # 6. 启动主做市巡航控制循环
        self._main_task = asyncio.create_task(self._main_loop())

    async def stop(self):
        if not self.is_running:
            return
        self.is_running = False
        self.log("WARN", "🛑 收到停止指令，正在撤销全部双向挂单...")
        try:
            await self.client.cancel_all_orders(self.config["contract"])
            self.active_quotes = {k: None for k in self.active_quotes}
        except Exception as e:
            self.log("ERROR", f"撤单异常: {e}")
            
        for t in [self._main_task, self._ws_task, self._sync_task]:
            if t and not t.done():
                t.cancel()
        self.log("INFO", "已安全停止做市服务")

    async def emergency_cancel(self):
        self.log("WARN", "🚨 触发一键紧急全撤单！")
        try:
            await self.client.cancel_all_orders(self.config["contract"])
            self.active_quotes = {k: None for k in self.active_quotes}
            self.log("INFO", "全合约挂单已清空")
        except Exception as e:
            self.log("ERROR", f"紧急撤单失败: {e}")

    # ==================== 1. WebSocket 毫秒级行情流 ====================
    async def _ws_orderbook_loop(self):
        ws_url = self.config.get("real_ws_url", "wss://fx-ws.gateio.ws/v4/ws/usdt")
        while self.is_running:
            try:
                session = await self.client._get_session()
                async with session.ws_connect(ws_url, timeout=aiohttp.ClientTimeout(total=6)) as ws:
                    sub_msg = {
                        "time": int(time.time()),
                        "channel": "futures.order_book",
                        "event": "subscribe",
                        "payload": [self.config["contract"], "5", "0"]
                    }
                    await ws.send_str(json.dumps(sub_msg))
                    self.log("INFO", "🔗 WebSocket 真实盘口行情流已建立连接")
                    
                    async for msg in ws:
                        if not self.is_running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("channel") == "futures.order_book" and data.get("result"):
                                res = data["result"]
                                bids = res.get("bids", [])
                                asks = res.get("asks", [])
                                if bids and asks:
                                    best_bid = float(bids[0]["p"])
                                    best_ask = float(asks[0]["p"])
                                    spread = max(0.01, best_ask - best_bid)
                                    mid_p = (best_bid + best_ask) / 2.0
                                    
                                    self.market_data.update({
                                        "best_bid": best_bid,
                                        "best_ask": best_ask,
                                        "spread_usdt": round(spread, 4),
                                        "spread_ticks": max(1, int(round(spread / self.config["tick_size"]))),
                                        "mid_price": round(mid_p, 4),
                                        "last_price": round(mid_p, 2),
                                        "orderbook_update_time": time.time(),
                                        "data_source": "WS_REALTIME"
                                    })
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except Exception as e:
                if self.is_running:
                    self.log("WARN", f"WebSocket 断开重连: {e}")
                    await asyncio.sleep(1.5)

    # ==================== 2. 全局多空双仓与账户极速同步 ====================
    async def _account_sync_loop(self):
        while self.is_running:
            try:
                acc_res, pos_res, trades_res = await asyncio.gather(
                    self.client.get_account(),
                    self.client.get_position(self.config["contract"]),
                    self.client.get_my_trades(self.config["contract"], limit=30),
                    return_exceptions=True
                )
                
                # 1. 账户资产
                if isinstance(acc_res, dict) and not acc_res.get("error"):
                    self.account_data["total_usdt"] = float(acc_res.get("total", 0.0))
                    self.account_data["available_usdt"] = float(acc_res.get("available", 0.0))
                    
                # 2. 深度同步多头仓位与空头仓位
                long_size = 0
                short_size = 0
                long_entry = 0.0
                short_entry = 0.0
                long_u = 0.0
                short_u = 0.0
                
                if isinstance(pos_res, list):
                    for p in pos_res:
                        m = p.get("mode")
                        sz = abs(int(p.get("size", 0)))
                        if m == "dual_long" and sz > 0:
                            long_size = sz
                            long_entry = float(p.get("entry_price", 0.0))
                            long_u = float(p.get("unrealised_pnl", 0.0))
                        elif m == "dual_short" and sz > 0:
                            short_size = sz
                            short_entry = float(p.get("entry_price", 0.0))
                            short_u = float(p.get("unrealised_pnl", 0.0))
                elif isinstance(pos_res, dict) and not pos_res.get("error"):
                    sz = int(pos_res.get("size", 0))
                    if sz > 0:
                        long_size = sz
                        long_entry = float(pos_res.get("entry_price", 0.0))
                        long_u = float(pos_res.get("unrealised_pnl", 0.0))
                    elif sz < 0:
                        short_size = abs(sz)
                        short_entry = float(pos_res.get("entry_price", 0.0))
                        short_u = float(pos_res.get("unrealised_pnl", 0.0))

                self.account_data.update({
                    "long_size": long_size,
                    "long_ltc": round(long_size * self.config["multiplier"], 2),
                    "short_size": short_size,
                    "short_ltc": round(short_size * self.config["multiplier"], 2),
                    "net_position": long_size - short_size,
                    "net_ltc": round((long_size - short_size) * self.config["multiplier"], 2),
                    "long_entry_price": long_entry,
                    "short_entry_price": short_entry,
                    "long_unreal_pnl": round(long_u, 4),
                    "short_unreal_pnl": round(short_u, 4),
                    "total_unrealised_pnl": round(long_u + short_u, 4),
                })
                
                # 同步离散开仓价位集合 (严格保证一个价位只开一次仓)
                if long_size == 0:
                    self.active_long_prices.clear()
                    self.last_long_trade_time = time.time()
                elif not self.active_long_prices and long_entry > 0:
                    self.active_long_prices.append(round(long_entry, 2))

                if short_size == 0:
                    self.active_short_prices.clear()
                    self.last_short_trade_time = time.time()
                elif not self.active_short_prices and short_entry > 0:
                    self.active_short_prices.append(round(short_entry, 2))
                
                # 3. 统计最新个人成交
                if isinstance(trades_res, list):
                    new_vol = 0.0
                    new_fees = 0.0
                    start_t = self.stats.get("start_timestamp") or 0
                    for tr in trades_res:
                        tr_id = tr.get("id")
                        tr_time = float(tr.get("create_time", 0))
                        if tr_id and tr_id not in self.known_trade_ids:
                            self.known_trade_ids.add(tr_id)
                            if tr_time < start_t:
                                continue
                            
                            sz = int(tr.get("size", 0))
                            abs_sz = abs(sz)
                            price = float(tr.get("price", 0.0))
                            vol = abs_sz * self.config["multiplier"] * price
                            role = tr.get("role", "maker")
                            fee = abs(float(tr.get("fee", 0.0)))
                            
                            self.stats["total_trades"] += 1
                            if sz > 0:
                                self.stats["long_trades"] += 1
                            else:
                                self.stats["short_trades"] += 1
                                
                            if role == "maker":
                                self.stats["maker_trades"] += 1
                            new_vol += vol
                            new_fees += fee
                            
                            side_text = "BUY" if sz > 0 else "SELL"
                            close_sz = int(tr.get("close_size", 0))
                            txt = tr.get("text", "")
                            
                            # 准确识别成交动作并更新开平仓价位集合与时间戳
                            if txt == "t-mm-openlong" or (not txt and sz > 0 and close_sz == 0):
                                action_text = "开多"
                                self.last_long_trade_time = time.time()
                                if price not in self.active_long_prices:
                                    self.active_long_prices.append(price)
                            elif txt == "t-mm-closelong" or (not txt and sz < 0 and close_sz > 0):
                                action_text = "平多"
                                self.last_long_trade_time = time.time()
                                if self.active_long_prices:
                                    self.active_long_prices.pop(0)
                            elif txt == "t-mm-openshort" or (not txt and sz < 0 and close_sz == 0):
                                action_text = "开空"
                                self.last_short_trade_time = time.time()
                                if price not in self.active_short_prices:
                                    self.active_short_prices.append(price)
                            elif txt == "t-mm-closeshort" or (not txt and sz > 0 and close_sz > 0):
                                action_text = "平空"
                                self.last_short_trade_time = time.time()
                                if self.active_short_prices:
                                    self.active_short_prices.pop(0)
                            else:
                                if close_sz > 0:
                                    action_text = "平空" if sz > 0 else "平多"
                                else:
                                    action_text = "开多" if sz > 0 else "开空"
                                
                            self.recent_trades.insert(0, {
                                "id": tr_id,
                                "time": datetime.fromtimestamp(tr_time).strftime("%H:%M:%S"),
                                "side": side_text,
                                "action": action_text,
                                "size_contracts": abs_sz,
                                "size_ltc": round(abs_sz * self.config["multiplier"], 2),
                                "price": price,
                                "volume_usdt": round(vol, 2),
                                "role": role,
                                "fee_usdt": round(fee, 4)
                            })
                            if len(self.recent_trades) > 50:
                                self.recent_trades.pop()
                            self.log("INFO", f"成交回报: [{action_text}] {side_text} {abs_sz}张 @ {price} (流水: {vol:.1f} U, {role})")
                            
                    if new_vol > 0:
                        self.stats["total_volume_usdt"] += new_vol
                        self.stats["nominal_fees_usdt"] += new_fees
                        self.stats["rebate_usdt"] = self.stats["nominal_fees_usdt"] * self.config["rebate_rate"]
                        self.stats["progress_pct"] = round((self.stats["total_volume_usdt"] / self.stats["target_volume_usdt"]) * 100, 4)
                        if self.stats["total_trades"] > 0:
                            self.stats["maker_ratio"] = round((self.stats["maker_trades"] / self.stats["total_trades"]) * 100, 1)

                # 实时计算真实账户净盈亏
                curr_eq = self.account_data["total_usdt"]
                init_eq = self.stats.get("initial_equity_usdt", curr_eq)
                real_diff = curr_eq - init_eq
                self.stats["net_pnl_usdt"] = round(real_diff + self.stats["rebate_usdt"], 4)
                self.stats["gross_profit_usdt"] = round(real_diff + self.stats["nominal_fees_usdt"], 4)
                
            except Exception as e:
                logger.error(f"Account sync error: {e}")
                
            await asyncio.sleep(1.0)

    def _find_unique_long_price(self, start_p: float, tick_size: float) -> float:
        """确保开多价位不与当前持有的多头各开仓价位重叠，严格一个价位开一次仓"""
        p = round(start_p, 2)
        long_size = self.account_data.get("long_size", 0)
        long_entry = self.account_data.get("long_entry_price", 0.0)
        
        for _ in range(15):
            conflict = False
            for held_p in self.active_long_prices:
                if abs(held_p - p) < tick_size * 0.6:
                    conflict = True
                    break
            if not conflict and long_size > 0 and long_entry > 0 and abs(long_entry - p) < tick_size * 0.6:
                conflict = True
            if not conflict:
                return p
            p = round(p - tick_size, 2)
        return p

    def _find_unique_short_price(self, start_p: float, tick_size: float) -> float:
        """确保开空价位不与当前持有的空头各开仓价位重叠，严格一个价位开一次仓"""
        p = round(start_p, 2)
        short_size = self.account_data.get("short_size", 0)
        short_entry = self.account_data.get("short_entry_price", 0.0)
        
        for _ in range(15):
            conflict = False
            for held_p in self.active_short_prices:
                if abs(held_p - p) < tick_size * 0.6:
                    conflict = True
                    break
            if not conflict and short_size > 0 and short_entry > 0 and abs(short_entry - p) < tick_size * 0.6:
                conflict = True
            if not conflict:
                return p
            p = round(p + tick_size, 2)
        return p

    # ==================== 3. 核心双向分档做市与挂单引擎 ====================
    async def _manage_dual_orders(self):
        """
        专业做市商分档挂单逻辑（严格遵循做市商第一性原理）：
        1. 一个价位开一次仓位：绝不把平仓和开仓挂在同一个价位，严格规避持仓价位重复开仓。
        2. 库存倾斜与双向常驻机制 (Inventory Skewing)：
           - 任何单边仓位为 0 时，该边开仓单必须提至盘口第一档 (买一/卖一) 优先撮合成交，保障双仓常驻！
           - 任何单边仓位偏重时，严禁在盘口第一档继续开同向仓位，必须后移至第二档或暂停，防止单边无限堆积！
        3. 保证所有活动订单的价格各不相同，成交明细中绝无同价同时开平！
        4. 价格硬性边界锁：严格消除 ORDER_POC_IMMEDIATE！
        5. 仓位与订单无时差并发同步：严格消除 REDUCE_EXCEEDED！
        """
        contract = self.config["contract"]
        tick_size = self.config.get("tick_size", 0.01)
        target_ticks = self.config.get("target_spread_ticks", 2)
        base_size = self.config.get("order_size_contracts", 10)
        max_inv = self.config.get("max_inventory_contracts", 30)
        net_max = self.config.get("max_net_inventory", 20)
        timeout_sec = self.config.get("auto_rebalance_timeout_sec", 30)

        # 1. 并发获取最新仓位与未完成订单 (确保持仓与挂单完全同源无时差)
        try:
            pos_res, open_orders_res = await asyncio.gather(
                self.client.get_position(contract),
                self.client.get_open_orders(contract),
                return_exceptions=True
            )
        except Exception as e:
            self.log("WARN", f"获取盘口状态异常: {e}")
            return

        if isinstance(pos_res, list):
            long_size = 0
            short_size = 0
            long_entry = 0.0
            short_entry = 0.0
            long_u = 0.0
            short_u = 0.0
            for p in pos_res:
                m = p.get("mode")
                sz = abs(int(p.get("size", 0)))
                if m == "dual_long" and sz > 0:
                    long_size = sz
                    long_entry = float(p.get("entry_price", 0.0))
                    long_u = float(p.get("unrealised_pnl", 0.0))
                elif m == "dual_short" and sz > 0:
                    short_size = sz
                    short_entry = float(p.get("entry_price", 0.0))
                    short_u = float(p.get("unrealised_pnl", 0.0))

            self.account_data.update({
                "long_size": long_size,
                "long_ltc": round(long_size * self.config["multiplier"], 2),
                "short_size": short_size,
                "short_ltc": round(short_size * self.config["multiplier"], 2),
                "net_position": long_size - short_size,
                "net_ltc": round((long_size - short_size) * self.config["multiplier"], 2),
                "long_entry_price": long_entry,
                "short_entry_price": short_entry,
                "long_unreal_pnl": round(long_u, 4),
                "short_unreal_pnl": round(short_u, 4),
                "total_unrealised_pnl": round(long_u + short_u, 4),
            })
            if long_size == 0:
                self.active_long_prices.clear()
                self.last_long_trade_time = time.time()
            elif not self.active_long_prices and long_entry > 0:
                self.active_long_prices.append(round(long_entry, 2))

            if short_size == 0:
                self.active_short_prices.clear()
                self.last_short_trade_time = time.time()
            elif not self.active_short_prices and short_entry > 0:
                self.active_short_prices.append(round(short_entry, 2))
        else:
            long_size = self.account_data.get("long_size", 0)
            short_size = self.account_data.get("short_size", 0)
            long_entry = self.account_data.get("long_entry_price", 0.0)
            short_entry = self.account_data.get("short_entry_price", 0.0)

        open_orders = open_orders_res if isinstance(open_orders_res, list) else []

        # 2. 获取盘口行情 (WS优先，REST回退)
        best_bid = self.market_data.get("best_bid", 0.0)
        best_ask = self.market_data.get("best_ask", 0.0)
        if best_bid <= 0 or best_ask <= 0 or (time.time() - self.market_data.get("orderbook_update_time", 0) > 4.0):
            ob = await self.client.get_orderbook(contract, limit=5)
            if isinstance(ob, dict) and "bids" in ob and "asks" in ob:
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                if bids and asks:
                    best_bid = float(bids[0]["p"])
                    best_ask = float(asks[0]["p"])
                    self.market_data["best_bid"] = best_bid
                    self.market_data["best_ask"] = best_ask
                    self.market_data["orderbook_update_time"] = time.time()
                    self.market_data["data_source"] = "REST_FALLBACK"

        if best_bid <= 0 or best_ask <= 0:
            return

        # 盘口倒挂保护
        if best_bid >= best_ask:
            best_bid = round(best_ask - tick_size, 2)
            if best_bid <= 0:
                return

        # 3. 核心做市基准定价
        bbo_bid = best_bid
        bbo_ask = max(best_ask, round(bbo_bid + target_ticks * tick_size, 2))
        bbo_bid = min(bbo_bid, round(bbo_ask - tick_size, 2))

        # 4. 统计在途开仓挂单，严格防止超额累计开仓
        pending_open_long = sum(int(o.get("size", 0)) for o in open_orders if o.get("text") == "t-mm-openlong")
        pending_open_short = sum(abs(int(o.get("size", 0))) for o in open_orders if o.get("text") == "t-mm-openshort")

        # 净敞口与单边最大持仓硬性上限风控
        can_open_long = (long_size + pending_open_long + base_size <= max_inv) and \
                        ((long_size - short_size + base_size) <= net_max)
        can_open_short = (short_size + pending_open_short + base_size <= max_inv) and \
                         ((short_size - long_size + base_size) <= net_max)

        # 5. 超时再平衡 / 库存倾斜判定
        now_t = time.time()
        long_rebalance = (now_t - self.last_long_trade_time > timeout_sec) or (long_size >= max_inv)
        short_rebalance = (now_t - self.last_short_trade_time > timeout_sec) or (short_size >= max_inv)

        desired_orders: Dict[str, Dict[str, Any]] = {}

        # ==================== 卖方通道 (Asks) ====================
        if short_size == 0 and can_open_short:
            # 空头缺失：卖一开空补齐底仓
            open_short_p = self._find_unique_short_price(bbo_ask, tick_size)
            desired_orders["t-mm-openshort"] = {
                "size": -base_size,
                "price": open_short_p,
                "reduce_only": False,
                "label": "开空做市 (卖一·补齐双仓)"
            }
            if long_size > 0:
                close_qty = min(base_size, long_size)
                if long_rebalance:
                    close_long_p = max(round(open_short_p + tick_size, 2), bbo_ask)
                else:
                    min_p = round(long_entry + tick_size, 2) if long_entry > 0 else round(bbo_ask + tick_size, 2)
                    close_long_p = max(round(open_short_p + tick_size, 2), min_p)
                desired_orders["t-mm-closelong"] = {
                    "size": -close_qty,
                    "price": close_long_p,
                    "reduce_only": True,
                    "label": "平多止盈 (卖二)"
                }
        else:
            has_close_long = False
            close_long_p = 0.0
            if long_size > 0:
                close_qty = min(base_size, long_size)
                if long_rebalance:
                    close_long_p = bbo_ask
                else:
                    close_long_p = max(bbo_ask, round(long_entry + tick_size, 2)) if long_entry > 0 else bbo_ask
                desired_orders["t-mm-closelong"] = {
                    "size": -close_qty,
                    "price": close_long_p,
                    "reduce_only": True,
                    "label": "平多止盈 (卖一)"
                }
                has_close_long = True

            if can_open_short:
                start_p = round(close_long_p + tick_size, 2) if has_close_long else bbo_ask
                open_short_p = self._find_unique_short_price(start_p, tick_size)
                if has_close_long and open_short_p <= close_long_p:
                    open_short_p = round(close_long_p + tick_size, 2)
                desired_orders["t-mm-openshort"] = {
                    "size": -base_size,
                    "price": open_short_p,
                    "reduce_only": False,
                    "label": f"开空做市 ({'卖二' if has_close_long else '卖一'})"
                }

        # ==================== 买方通道 (Bids) ====================
        if long_size == 0 and can_open_long:
            # 多头缺失：买一开多补齐底仓
            open_long_p = self._find_unique_long_price(bbo_bid, tick_size)
            desired_orders["t-mm-openlong"] = {
                "size": base_size,
                "price": open_long_p,
                "reduce_only": False,
                "label": "开多做市 (买一·补齐双仓)"
            }
            if short_size > 0:
                close_qty = min(base_size, short_size)
                if short_rebalance:
                    close_short_p = min(round(open_long_p - tick_size, 2), bbo_bid)
                else:
                    max_p = round(short_entry - tick_size, 2) if short_entry > 0 else round(bbo_bid - tick_size, 2)
                    close_short_p = min(round(open_long_p - tick_size, 2), max_p)
                desired_orders["t-mm-closeshort"] = {
                    "size": close_qty,
                    "price": close_short_p,
                    "reduce_only": True,
                    "label": "平空止盈 (买二)"
                }
        else:
            has_close_short = False
            close_short_p = 0.0
            if short_size > 0:
                close_qty = min(base_size, short_size)
                if short_rebalance:
                    close_short_p = bbo_bid
                else:
                    close_short_p = min(bbo_bid, round(short_entry - tick_size, 2)) if short_entry > 0 else bbo_bid
                desired_orders["t-mm-closeshort"] = {
                    "size": close_qty,
                    "price": close_short_p,
                    "reduce_only": True,
                    "label": "平空止盈 (买一)"
                }
                has_close_short = True

            if can_open_long:
                start_p = round(close_short_p - tick_size, 2) if has_close_short else round(bbo_bid - tick_size, 2)
                open_long_p = self._find_unique_long_price(start_p, tick_size)
                if has_close_short and open_long_p >= close_short_p:
                    open_long_p = round(close_short_p - tick_size, 2)
                desired_orders["t-mm-openlong"] = {
                    "size": base_size,
                    "price": open_long_p,
                    "reduce_only": False,
                    "label": "开多做市 (买二)"
                }

        # 6. Maker Post-Only 价格硬性边界保护锁 (彻底消除 ORDER_POC_IMMEDIATE)
        max_maker_bid = round(best_ask - tick_size, 2)
        min_maker_ask = round(best_bid + tick_size, 2)
        for txt, target in list(desired_orders.items()):
            sz = target["size"]
            p = round(target["price"], 2)
            if sz > 0:  # 买单
                if p > max_maker_bid:
                    p = max_maker_bid
            elif sz < 0:  # 卖单
                if p < min_maker_ask:
                    p = min_maker_ask
            if p <= 0:
                del desired_orders[txt]
                continue
            target["price"] = p

        # 7. 对账与智能增删改挂单
        existing_by_text = {}
        orders_to_cancel = []
        for o in open_orders:
            txt = o.get("text", "")
            if txt in desired_orders and txt not in existing_by_text:
                existing_by_text[txt] = o
            else:
                orders_to_cancel.append(o.get("id"))

        orders_to_place = []
        for txt, target in desired_orders.items():
            if txt in existing_by_text:
                o = existing_by_text[txt]
                o_p = float(o.get("price", 0.0))
                o_sz = int(o.get("size", 0))
                o_red = o.get("is_reduce_only", False)
                if abs(o_p - target["price"]) < tick_size * 0.6 and o_sz == target["size"] and o_red == target["reduce_only"]:
                    self.active_quotes[txt] = {
                        "id": o["id"], "price": o_p, "size": abs(o_sz), "reduce_only": o_red, "label": target["label"]
                    }
                    continue
                else:
                    orders_to_cancel.append(o.get("id"))

            orders_to_place.append((txt, target))

        # 执行撤单
        if orders_to_cancel:
            for oid in orders_to_cancel:
                try:
                    await self.client.cancel_order(oid)
                except Exception:
                    pass

        # 执行新挂单
        for txt, target in orders_to_place:
            try:
                res = await self.client.place_order(
                    contract=contract,
                    size=target["size"],
                    price=target["price"],
                    tif="poc",
                    text=txt,
                    reduce_only=target["reduce_only"]
                )
                if isinstance(res, dict):
                    if not res.get("error") and res.get("id"):
                        self.active_quotes[txt] = {
                            "id": res["id"],
                            "price": target["price"],
                            "size": abs(target["size"]),
                            "reduce_only": target["reduce_only"],
                            "label": target["label"]
                        }
                        self.log("INFO", f"📍 挂单成功 [{target['label']}]: {abs(target['size'])}张 @ {target['price']} (Maker POC)")
                    else:
                        # 拦截并自愈 REDUCE_EXCEEDED (杜绝刷屏)
                        lbl = res.get("label", "")
                        if "REDUCE_EXCEEDED" in lbl:
                            self.log("WARN", f"平仓单越界自愈: {target['label']}, 交易所持仓已清空")
                            if "closelong" in txt:
                                self.account_data["long_size"] = 0
                                self.active_long_prices.clear()
                            elif "closeshort" in txt:
                                self.account_data["short_size"] = 0
                                self.active_short_prices.clear()
                        elif "ORDER_POC_IMMEDIATE" in lbl:
                            self.log("DEBUG", f"盘口微秒跳变触发POC保护，稍后自动重挂: {lbl}")
                        else:
                            self.log("WARN", f"挂单失败 [{target['label']}]: {lbl or res.get('message')}")
                        self.active_quotes[txt] = None
            except Exception as e:
                self.log("ERROR", f"挂单异常 [{target['label']}]: {e}")
                self.active_quotes[txt] = None

        # 清理不再需要的 active_quotes slot
        for k in list(self.active_quotes.keys()):
            if k.startswith("t-mm-") and k not in desired_orders:
                self.active_quotes[k] = None

        # 兼容老前端字段
        self.active_quotes["bid"] = self.active_quotes.get("t-mm-closeshort") or self.active_quotes.get("t-mm-openlong")
        self.active_quotes["ask"] = self.active_quotes.get("t-mm-closelong") or self.active_quotes.get("t-mm-openshort")

    # ==================== 4. 主做市巡航循环 ====================
    async def _main_loop(self):
        self.log("INFO", "高频双向对冲 V4 做市巡航循环就绪")
        while self.is_running:
            try:
                await self._manage_dual_orders()
                if self.stats["start_timestamp"]:
                    self.stats["running_time_seconds"] = int(time.time() - self.stats["start_timestamp"])
            except Exception as e:
                self.log("ERROR", f"做市主循环异常: {e}")
                
            await asyncio.sleep(0.5)

    def get_full_status(self) -> Dict[str, Any]:
        return {
            "is_running": self.is_running,
            "stats": self.stats,
            "market_data": self.market_data,
            "account_data": self.account_data,
            "active_quotes": self.active_quotes,
            "recent_trades": self.recent_trades[:25],
            "audit_logs": self.audit_logs[:40],
            "config": {
                "contract": self.config["contract"],
                "target_spread_ticks": self.config["target_spread_ticks"],
                "order_size_contracts": self.config["order_size_contracts"],
                "max_inventory_contracts": self.config.get("max_inventory_contracts", 30),
                "max_net_inventory": self.config.get("max_net_inventory", 20),
                "rebate_rate": self.config["rebate_rate"],
                "target_volume_usdt": self.config["target_volume_usdt"],
            }
        }
