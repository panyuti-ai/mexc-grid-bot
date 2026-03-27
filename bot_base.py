"""
╔══════════════════════════════════════════════════════════════════╗
║         Jane Street v16 — Bot Base（共用邏輯模組）               ║
║                                                                  ║
║  架構說明：                                                      ║
║    bot_base.py  → 所有策略邏輯（只需維護這一份）                 ║
║    bot_eth.py   → ETH 參數設定 + 啟動入口                       ║
║    bot_sol.py   → SOL 參數設定 + 啟動入口                       ║
║                                                                  ║
║  v16 變更：                                                      ║
║    ✅ 動態買入倍率（Dynamic Position Sizing）                    ║
║       啟動時輸入價格上下限                                       ║
║       越接近下限 → 買入量放大（最高 2.5x）                       ║
║       越接近上限 → 買入量縮小（最低 0.5x）                       ║
║       超出範圍 → 停止買入                                        ║
║    ✅ process_sell_layer 修正：                                  ║
║       成交檢查不受 max_sell 限制（全部都能偵測到）               ║
║       max_sell 只限制「重掛賣單」的數量                          ║
║       API 回傳 unknown 時跳過，不誤觸 timeout                   ║
║                                                                  ║
║  v15 已修正（保留）：                                            ║
║    ✅ File Lock / cancel 回寫 / fill_rate 累計                  ║
║    ✅ Crash Guard 300s / fallback False / JSON 損壞保護          ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import time
import json
import fcntl
import shutil
import logging
import requests
from datetime import datetime, date
from collections import deque
from dotenv import load_dotenv
import okx.MarketData as MarketData
import okx.Trade as Trade
import okx.Account as Account

load_dotenv()

API_KEY             = os.environ.get("OKX_API_KEY")
SECRET_KEY          = os.environ.get("OKX_SECRET_KEY")
PASSPHRASE          = os.environ.get("OKX_PASSPHRASE")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# ── 共用系統參數 ──
TAKER_FEE           = 0.001
MAKER_FEE           = 0.0008
LOOP_INTERVAL       = 2
MAX_RETRIES         = 3
FILL_WAIT_SECS      = 2
BALANCE_INTERVAL    = 10
MIN_BUY_AMOUNT      = 10.0
API_CALL_DELAY      = 0.12
OB_DEPTH            = 20
TFI_LIMIT           = 50
OBI_REFRESH_SECS    = 10
MAX_BUY_10S         = 4
MAKER_TIMEOUT_SECS  = 120
PRICE_MOVE_TIMEOUT  = 0.005    # 0.5%（原本 0.15% 太敏感，會瘋狂重掛）
MAX_INVENTORY       = 0.65
MIN_INVENTORY       = 0.15
INVENTORY_SKEW_THRESHOLD = 0.50
EMA_FAST            = 20
EMA_SLOW            = 50
ATR_PERIOD          = 14
CRASH_OBI           = 0.15
CRASH_TFI           = 0.15
CRASH_COOLDOWN_SECS = 60
CRASH_MAX_COOLDOWN  = 300
CRASH_RECOVERY_OBI  = 0.3
CRASH_STOP_DROP     = 0.003

# ── OKX API ──
marketAPI  = MarketData.MarketAPI(flag="0")
tradeAPI   = Trade.TradeAPI(API_KEY, SECRET_KEY, PASSPHRASE, flag="0")
accountAPI = Account.AccountAPI(API_KEY, SECRET_KEY, PASSPHRASE, flag="0")

USDT_LOCK_FILE = "/tmp/trading_bot_usdt.lock"

def api_delay():
    time.sleep(API_CALL_DELAY)


# ══════════════════════════════════════════════════════════════════
#  啟動時價格範圍輸入（由 bot_eth.py / bot_sol.py 呼叫）
# ══════════════════════════════════════════════════════════════════
def ask_price_range(config):
    """啟動時讓使用者輸入價格上下限，寫入 config"""
    symbol = config["SYMBOL"]
    coin   = config["COIN"]

    # 抓目前市價給使用者參考
    log = logging.getLogger("SETUP")
    try:
        current = get_market_price(symbol, log)
        print(f"\n  📊 {symbol} 目前市價：${current:.2f}")
    except Exception:
        current = None
        print(f"\n  ⚠️  無法取得 {symbol} 即時價格")

    print(f"\n  ┌─────────────────────────────────────────────┐")
    print(f"  │  動態買入倍率（Dynamic Position Sizing）     │")
    print(f"  │                                             │")
    print(f"  │  越接近下限 → 買入量最多 {config['DYNAMIC_BUY_MAX_MULT']}x              │")
    print(f"  │  越接近上限 → 買入量最少 {config['DYNAMIC_BUY_MIN_MULT']}x              │")
    print(f"  │  超出範圍   → 完全停止買入                  │")
    print(f"  └─────────────────────────────────────────────┘\n")

    # 輸入下限
    while True:
        try:
            raw = input(f"  請輸入 {coin} 價格下限（例如 {'67' if coin == 'SOL' else '1500'}）: ").strip()
            min_price = float(raw)
            if min_price <= 0:
                print("  ⚠️  價格必須大於 0")
                continue
            break
        except ValueError:
            print("  ⚠️  請輸入有效數字")

    # 輸入上限
    while True:
        try:
            raw = input(f"  請輸入 {coin} 價格上限（例如 {'155' if coin == 'SOL' else '3000'}）: ").strip()
            max_price = float(raw)
            if max_price <= min_price:
                print(f"  ⚠️  上限必須大於下限 ${min_price:.2f}")
                continue
            break
        except ValueError:
            print("  ⚠️  請輸入有效數字")

    config["TRADE_MIN_PRICE"] = min_price
    config["TRADE_MAX_PRICE"] = max_price

    # 顯示確認
    inner_base = config["INNER_CAPITAL"] / config["INNER_MAX_BATCH"]
    min_mult   = config["DYNAMIC_BUY_MIN_MULT"]
    max_mult   = config["DYNAMIC_BUY_MAX_MULT"]
    mid        = (min_price + max_price) / 2
    mid_mult   = (min_mult + max_mult) / 2

    print(f"\n  ✅ 設定完成！")
    print(f"  ┌──────────────────────────────────────────────┐")
    print(f"  │  交易範圍：${min_price:.2f} ~ ${max_price:.2f}")
    print(f"  │  ──────────────────────────────              │")
    print(f"  │  ${max_price:>8.2f}（上限）→ {min_mult:.1f}x → ~${inner_base * min_mult:.2f}/筆")
    print(f"  │  ${mid:>8.2f}（中間）→ {mid_mult:.1f}x → ~${inner_base * mid_mult:.2f}/筆")
    print(f"  │  ${min_price:>8.2f}（下限）→ {max_mult:.1f}x → ~${inner_base * max_mult:.2f}/筆")
    print(f"  │  超出範圍 → 停止買入")
    print(f"  └──────────────────────────────────────────────┘\n")

    confirm = input("  按 Enter 啟動 Bot（或輸入 q 取消）: ").strip()
    if confirm.lower() == "q":
        print("  ⛔ 已取消")
        exit(0)


# ══════════════════════════════════════════════════════════════════
#  File Lock
# ══════════════════════════════════════════════════════════════════
def acquire_usdt_lock(timeout=3.0):
    deadline = time.time() + timeout
    lock_file = open(USDT_LOCK_FILE, "w")
    while time.time() < deadline:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except BlockingIOError:
            time.sleep(0.1)
    lock_file.close()
    return None

def release_usdt_lock(lock_file):
    if lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
#  Discord 通報
# ══════════════════════════════════════════════════════════════════
def make_discord_sender(symbol):
    def send(msg: str):
        if not DISCORD_WEBHOOK_URL:
            return
        try:
            requests.post(
                DISCORD_WEBHOOK_URL,
                json={"content": f"[{symbol}] {msg}"},
                timeout=5
            )
        except Exception as e:
            logging.warning(f"Discord 失敗：{e}")
    return send


# ══════════════════════════════════════════════════════════════════
#  模組一：市場引擎（ATR + EMA + Volatility Regime）
# ══════════════════════════════════════════════════════════════════
class MarketEngine:
    def __init__(self, cfg):
        self.cfg     = cfg
        self.closes  = deque(maxlen=EMA_SLOW + 1)
        self.tr_list = deque(maxlen=ATR_PERIOD)
        self.prev_close = None
        self.atr = None

    def update(self, high, low, close):
        self.closes.append(close)
        if self.prev_close is not None:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
            self.tr_list.append(tr)
            if len(self.tr_list) >= ATR_PERIOD:
                self.atr = sum(self.tr_list) / len(self.tr_list)
        self.prev_close = close

    def _ema(self, period):
        if len(self.closes) < period:
            return None
        prices = list(self.closes)[-period:]
        k = 2 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema

    def get_trend(self):
        fast = self._ema(EMA_FAST)
        slow = self._ema(EMA_SLOW)
        if fast is not None and slow is not None and fast < slow:
            return "down", fast, slow
        return "normal", fast, slow

    def get_vol_regime(self, price):
        if self.atr is None or price == 0:
            return "normal"
        vol = self.atr / price
        if vol < self.cfg["VOL_LOW"]:   return "low"
        elif vol < self.cfg["VOL_HIGH"]: return "normal"
        else:                            return "high"

    def get_inner_spacing(self, price, is_crash, trend):
        cfg = self.cfg
        if is_crash or trend == "down":
            return cfg["INNER_BASE"] * cfg["INNER_TREND_MULT"]
        regime = self.get_vol_regime(price)
        if regime == "low":
            return cfg["INNER_LOW_VOL"]
        elif regime == "high" and self.atr is not None:
            return max(cfg["INNER_BASE"], self.atr / price)
        return cfg["INNER_BASE"]

    def get_outer_spacing(self, is_crash, trend):
        cfg = self.cfg
        if is_crash or trend == "down":
            return cfg["OUTER_BASE"] * cfg["OUTER_TREND_MULT"]
        return cfg["OUTER_BASE"]


# ══════════════════════════════════════════════════════════════════
#  模組二：Crash Guard
# ══════════════════════════════════════════════════════════════════
class CrashGuard:
    def __init__(self, cfg, send_discord):
        self.cfg            = cfg
        self.send           = send_discord
        self.price_history  = deque(maxlen=30)
        self.cooldown_until = 0
        self.crash_low      = None
        self.cooldown_start = 0

    def add_price(self, price):
        self.price_history.append(price)

    def get_price_drop_1m(self, current_price):
        if len(self.price_history) < 5:
            return 0.0
        max_p = max(self.price_history)
        return (max_p - current_price) / max_p if max_p > 0 else 0.0

    def get_price_recovery(self, current_price):
        if not self.crash_low or self.crash_low == 0:
            return 0.0
        return (current_price - self.crash_low) / self.crash_low

    def check(self, current_price, obi, tfi, log) -> bool:
        now           = time.time()
        price_drop_1m = self.get_price_drop_1m(current_price)

        if (obi < CRASH_OBI and tfi < CRASH_TFI and
                price_drop_1m > self.cfg["CRASH_DROP_1M"]):
            if now > self.cooldown_until:
                self.crash_low      = current_price
                self.cooldown_start = now
                self.cooldown_until = now + CRASH_COOLDOWN_SECS
                log.critical(f"🚨 Crash Guard！OBI:{obi:.2f} TFI:{tfi:.2f} 跌幅:{price_drop_1m*100:.2f}%")
                self.send(
                    f"🚨 **崩盤警報！**\n"
                    f"OBI：{obi:.2f} | TFI：{tfi:.2f}\n"
                    f"60秒跌幅：{price_drop_1m*100:.2f}%"
                )

        if now <= self.cooldown_until:
            if now - self.cooldown_start > CRASH_MAX_COOLDOWN:
                log.warning(f"⚠️  Crash Guard 強制解除（超過{CRASH_MAX_COOLDOWN}秒）")
                self.send(f"⚠️ **Crash Guard 強制解除（超過{CRASH_MAX_COOLDOWN}秒）**")
                self.cooldown_until = 0
                self.crash_low      = None
                return False
            return True

        if self.cooldown_until > 0:
            recovery     = self.get_price_recovery(current_price)
            price_stable = price_drop_1m < CRASH_STOP_DROP
            if price_stable and (recovery > self.cfg["CRASH_RECOVERY_PRICE"]
                                 or obi > CRASH_RECOVERY_OBI):
                log.info(f"✅ Crash Guard 解除｜回升:{recovery*100:.2f}%")
                self.send(f"✅ **崩盤警報解除**｜回升：{recovery*100:.2f}%")
                self.cooldown_until = 0
                self.crash_low      = None
            else:
                return True

        return False


# ══════════════════════════════════════════════════════════════════
#  模組三：防插針連買
# ══════════════════════════════════════════════════════════════════
class BuyRateGuard:
    def __init__(self):
        self.buy_times = deque()

    def can_buy(self) -> bool:
        now = time.time()
        while self.buy_times and self.buy_times[0] < now - 10:
            self.buy_times.popleft()
        return len(self.buy_times) < MAX_BUY_10S

    def record_buy(self):
        self.buy_times.append(time.time())


# ══════════════════════════════════════════════════════════════════
#  模組四：OBI/TFI 快取
# ══════════════════════════════════════════════════════════════════
class OrderFlowCache:
    def __init__(self, symbol):
        self.symbol       = symbol
        self.obi          = 0.5
        self.tfi          = 0.5
        self.last_updated = 0

    def update(self, log):
        now = time.time()
        if now - self.last_updated < OBI_REFRESH_SECS:
            return
        try:
            api_delay()
            data    = marketAPI.get_orderbook(instId=self.symbol, sz=str(OB_DEPTH))["data"][0]
            bid_vol = sum(float(b[1]) for b in data["bids"])
            ask_vol = sum(float(a[1]) for a in data["asks"])
            total   = bid_vol + ask_vol
            if total > 0:
                self.obi = bid_vol / total
        except Exception as e:
            log.warning(f"OBI 更新失敗：{e}")
        try:
            api_delay()
            data   = marketAPI.get_trades(instId=self.symbol, limit=str(TFI_LIMIT))["data"]
            buy_v  = sum(float(t["sz"]) for t in data if t["side"] == "buy")
            sell_v = sum(float(t["sz"]) for t in data if t["side"] == "sell")
            total  = buy_v + sell_v
            if total > 0:
                self.tfi = buy_v / total
        except Exception as e:
            log.warning(f"TFI 更新失敗：{e}")
        self.last_updated = now


# ══════════════════════════════════════════════════════════════════
#  報價計算
# ══════════════════════════════════════════════════════════════════
def get_mid_price(symbol, tick_size, log):
    try:
        api_delay()
        data     = marketAPI.get_orderbook(instId=symbol, sz="1")["data"][0]
        best_bid = float(data["bids"][0][0])
        best_ask = float(data["asks"][0][0])
        return (best_bid + best_ask) / 2, best_bid, best_ask
    except Exception as e:
        log.warning(f"get_mid_price 失敗：{e}")
        return 0.0, 0.0, 0.0

def get_buy_trigger(last_price, mid_price, last_buy_price, grid_spacing):
    if last_buy_price is not None:
        return last_buy_price * (1 - grid_spacing), last_buy_price
    base = mid_price if mid_price > 0 else last_price
    return base * (1 - grid_spacing), base

def get_sell_spacing(inv_ratio, grid_spacing, skew_spacing):
    return skew_spacing if inv_ratio > INVENTORY_SKEW_THRESHOLD else grid_spacing

def get_maker_sell_price(buy_price, sell_spacing, best_ask, best_bid, layer, tick_size, log):
    min_price = buy_price * (1 + TAKER_FEE + MAKER_FEE + 0.0002)

    if layer == "outer":
        return round(max(buy_price * (1 + sell_spacing), min_price), 2)

    try:
        if best_bid > 0 and best_ask > 0:
            spread = best_ask - best_bid
            if spread > 2 * tick_size:
                micro_price = best_bid + spread * 0.6
                if micro_price >= min_price:
                    return round(micro_price, 2)
            maker_price = best_ask - tick_size
            if maker_price >= min_price:
                return round(maker_price, 2)
    except Exception as e:
        log.warning(f"Microspread 計算失敗：{e}")

    return round(max(buy_price * (1 + sell_spacing), min_price), 2)


# ══════════════════════════════════════════════════════════════════
#  批次管理（Atomic Write + JSON 損壞保護）
# ══════════════════════════════════════════════════════════════════
def load_batches(batches_file, layer=None, log=None):
    for filepath in [batches_file, batches_file + ".bak"]:
        if os.path.exists(filepath):
            try:
                with open(filepath) as f:
                    all_b = json.load(f)
                if layer:
                    return [b for b in all_b if b.get("layer") == layer]
                return all_b
            except json.JSONDecodeError as e:
                if log:
                    log.error(f"JSON 損壞（{filepath}）：{e}，嘗試備份...")
    return []

def save_batches(batches, batches_file):
    """✅ Atomic Write + 自動備份 + 空值保護"""
    if batches is None:
        batches = []
    if not isinstance(batches, list):
        return  # 防止寫入非法資料
    if os.path.exists(batches_file):
        shutil.copy2(batches_file, batches_file + ".bak")
    tmp = batches_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(batches, f, indent=2)
    os.replace(tmp, batches_file)

def add_batch(fill_price, fill_qty, buy_amount, layer, symbol,
              batches_file, log, sell_order_id=None, sell_price=None):
    all_b  = load_batches(batches_file)
    max_id = max([b["id"] for b in all_b], default=0)
    batch  = {
        "id":             max_id + 1,
        "symbol":         symbol,
        "layer":          layer,
        "buy_price":      fill_price,
        "qty":            fill_qty,
        "buy_usdt":       round(fill_price * fill_qty, 6),
        "buy_amount":     buy_amount,
        "buy_time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sell_order_id":  sell_order_id,
        "sell_price":     sell_price,
        "sell_placed_at": time.time() if sell_order_id else None,
    }
    all_b.append(batch)
    save_batches(all_b, batches_file)
    log.info(f"📝 [{layer.upper()}] 批次#{batch['id']}｜買:${fill_price:.2f} x {fill_qty:.6f}｜掛賣:${sell_price}")
    return batch

def update_batch_sell(batch_id, sell_order_id, sell_price, batches_file):
    all_b = load_batches(batches_file)
    for b in all_b:
        if b["id"] == batch_id:
            b["sell_order_id"]  = sell_order_id
            b["sell_price"]     = sell_price
            b["sell_placed_at"] = time.time()
    save_batches(all_b, batches_file)

def remove_batch(batch_id, batches_file):
    save_batches([b for b in load_batches(batches_file) if b["id"] != batch_id], batches_file)

def clear_batch_sell_order(batch_id, batches_file):
    all_b = load_batches(batches_file)
    for b in all_b:
        if b["id"] == batch_id:
            b["sell_order_id"]  = None
            b["sell_placed_at"] = None
    save_batches(all_b, batches_file)


# ══════════════════════════════════════════════════════════════════
#  統計 & Metrics
# ══════════════════════════════════════════════════════════════════
def load_stats(stats_file):
    if os.path.exists(stats_file):
        try:
            with open(stats_file) as f:
                return json.load(f)
        except Exception:
            pass
    return {"total_profit": 0.0, "total_trades": 0, "daily": {}}

def save_stats(s, stats_file):
    tmp = stats_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, stats_file)

def record_profit(profit, layer, stats_file):
    s     = load_stats(stats_file)
    today = str(date.today())
    if today not in s["daily"]:
        s["daily"][today] = {"trades": 0, "profit": 0.0,
                             "inner": 0.0, "outer": 0.0, "fees": 0.0}
    s["daily"][today]["trades"] += 1
    s["daily"][today]["profit"]  = round(s["daily"][today]["profit"] + profit, 6)
    s["daily"][today][layer]     = round(s["daily"][today].get(layer, 0.0) + profit, 6)
    s["total_trades"] += 1
    s["total_profit"]  = round(s["total_profit"] + profit, 6)
    save_stats(s, stats_file)

def get_today_stats(stats_file):
    s = load_stats(stats_file)
    d = s["daily"].get(str(date.today()),
                       {"trades": 0, "profit": 0.0, "inner": 0.0, "outer": 0.0})
    return d["profit"], d["trades"], d.get("inner", 0.0), d.get("outer", 0.0), s["total_profit"]

def update_metrics(trades, fees, pnl, inv, maker_placed, maker_filled, metrics_file, symbol):
    today     = str(date.today())
    fill_rate = (maker_filled / maker_placed * 100) if maker_placed > 0 else 0.0
    metrics   = {}
    if os.path.exists(metrics_file):
        try:
            with open(metrics_file) as f:
                metrics = json.load(f)
        except Exception:
            pass
    metrics[today] = {
        "symbol":          symbol,
        "trades_count":    trades,
        "fees_paid":       round(fees, 6),
        "net_pnl":         round(pnl, 6),
        "inventory_ratio": round(inv, 4),
        "maker_placed":    maker_placed,
        "maker_filled":    maker_filled,
        "maker_fill_rate": round(fill_rate, 2),
    }
    tmp = metrics_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(metrics, f, indent=2)
    os.replace(tmp, metrics_file)
    return fill_rate


# ══════════════════════════════════════════════════════════════════
#  市場資料 & 帳戶
# ══════════════════════════════════════════════════════════════════
def get_market_price(symbol, log):
    for i in range(MAX_RETRIES):
        try:
            api_delay()
            return float(marketAPI.get_ticker(instId=symbol)["data"][0]["last"])
        except Exception as e:
            log.warning(f"價格失敗({i+1})：{e}")
            time.sleep(1)
    raise RuntimeError(f"[{symbol}] 無法取得市場價格")

def get_candle(symbol, log):
    for i in range(MAX_RETRIES):
        try:
            api_delay()
            c = marketAPI.get_candlesticks(instId=symbol, bar="15m", limit="2")["data"][0]
            return float(c[2]), float(c[3]), float(c[4])
        except Exception as e:
            log.warning(f"K線失敗({i+1})：{e}")
            time.sleep(1)
    return 0.0, 0.0, 0.0

def get_balances(coin, log):
    try:
        api_delay()
        details        = accountAPI.get_account_balance()["data"][0]["details"]
        usdt, coin_bal = 0.0, 0.0
        for item in details:
            if item["ccy"] == "USDT": usdt     = float(item["availBal"])
            elif item["ccy"] == coin: coin_bal  = float(item["availBal"])
        return usdt, coin_bal
    except Exception as e:
        log.error(f"餘額失敗：{e}")
        return 0.0, 0.0

def get_inv(usdt, coin_bal, price):
    total = coin_bal * price + usdt
    return (coin_bal * price / total) if total > 0 else 0.5


# ══════════════════════════════════════════════════════════════════
#  ✅ v16：動態買入倍率計算
#
#  原理：價格在使用者設定的區間內線性映射倍率
#  price_ratio = 0.0（底部）→ multiplier = max_mult（買最多）
#  price_ratio = 1.0（頂部）→ multiplier = min_mult（買最少）
#
#  保留 capital/remaining 的遞增邏輯，動態倍率疊在上面
#  兩者相乘 = 越低位、越後期，每筆金額越大
# ══════════════════════════════════════════════════════════════════
def calc_buy_amount(layer, count, cfg, current_price):
    capital   = cfg["INNER_CAPITAL"] if layer == "inner" else cfg["OUTER_CAPITAL"]
    max_batch = cfg["INNER_MAX_BATCH"] if layer == "inner" else cfg["OUTER_MAX_BATCH"]
    remaining = max_batch - count

    if remaining <= 0:
        return MIN_BUY_AMOUNT

    # 基準：保留 capital/remaining 的遞增邏輯
    base_amount = capital / remaining

    # 動態倍率
    min_p    = cfg.get("TRADE_MIN_PRICE")
    max_p    = cfg.get("TRADE_MAX_PRICE")
    min_mult = cfg.get("DYNAMIC_BUY_MIN_MULT", 0.5)
    max_mult = cfg.get("DYNAMIC_BUY_MAX_MULT", 2.5)

    if min_p and max_p and max_p > min_p and min_p < current_price < max_p:
        # 0.0 = 底部（買最多），1.0 = 頂部（買最少）
        price_ratio = (current_price - min_p) / (max_p - min_p)
        multiplier  = max_mult - price_ratio * (max_mult - min_mult)
        final_amount = base_amount * multiplier
    else:
        # 超出範圍或未設定 → 用基準值（買入會被 is_price_in_range 擋住）
        final_amount = base_amount

    return max(final_amount, MIN_BUY_AMOUNT)

def get_dynamic_multiplier(cfg, current_price):
    """計算目前價格對應的倍率（給 log 顯示用）"""
    min_p    = cfg.get("TRADE_MIN_PRICE")
    max_p    = cfg.get("TRADE_MAX_PRICE")
    min_mult = cfg.get("DYNAMIC_BUY_MIN_MULT", 0.5)
    max_mult = cfg.get("DYNAMIC_BUY_MAX_MULT", 2.5)

    if min_p and max_p and max_p > min_p and min_p < current_price < max_p:
        price_ratio = (current_price - min_p) / (max_p - min_p)
        return max_mult - price_ratio * (max_mult - min_mult)
    return 1.0

def is_price_in_range(cfg, current_price):
    """✅ 價格邊界檢查：超出範圍就停止買入"""
    min_p = cfg.get("TRADE_MIN_PRICE")
    max_p = cfg.get("TRADE_MAX_PRICE")
    if min_p is not None and current_price <= min_p:
        return False
    if max_p is not None and current_price >= max_p:
        return False
    return True

def calc_pool_spent(batches_file, layer, cfg):
    all_b   = load_batches(batches_file)
    capital = cfg["INNER_CAPITAL"] if layer == "inner" else cfg["OUTER_CAPITAL"]
    spent   = sum(b.get("buy_amount", 0) for b in all_b if b.get("layer") == layer)
    return spent, capital - spent


# ══════════════════════════════════════════════════════════════════
#  執行訂單
# ══════════════════════════════════════════════════════════════════
def execute_taker_buy(buy_amount, symbol, log, send_discord):
    for attempt in range(MAX_RETRIES):
        try:
            api_delay()
            result = tradeAPI.place_order(
                instId=symbol, tdMode="cash", side="buy",
                ordType="market", sz=str(round(buy_amount, 4)), tgtCcy="quote_ccy"
            )
            if result.get("code") == "0":
                order_id = result["data"][0]["ordId"]
                time.sleep(FILL_WAIT_SECS)
                for _ in range(MAX_RETRIES):
                    api_delay()
                    d       = tradeAPI.get_order(instId=symbol, ordId=order_id)["data"][0]
                    avg_px  = float(d.get("avgPx") or 0)
                    fill_sz = float(d.get("fillSz") or 0)
                    if avg_px > 0 and d.get("state") == "filled":
                        log.info(f"✅ 買入 ${avg_px:.2f} x {fill_sz:.6f}")
                        return True, avg_px, fill_sz
                    time.sleep(FILL_WAIT_SECS)
                # 幽靈倉位警告
                warn_msg = (
                    f"⚠️ **幽靈倉位警告！**\n"
                    f"訂單 #{order_id} 已送出但查詢逾時\n"
                    f"請手動到 OKX 確認是否成交，並更新 batches JSON"
                )
                log.warning(f"⚠️ 幽靈倉位警告！訂單#{order_id} 已送出但查詢逾時")
                send_discord(warn_msg)
                return False, 0.0, 0.0
            else:
                log.warning(f"買入失敗({attempt+1})：{result.get('msg')}")
                time.sleep(2)
        except Exception as e:
            log.warning(f"買入例外({attempt+1})：{e}")
            time.sleep(2)
    return False, 0.0, 0.0

def place_maker_sell(qty, sell_price, symbol, log):
    for attempt in range(MAX_RETRIES):
        try:
            api_delay()
            result = tradeAPI.place_order(
                instId=symbol, tdMode="cash", side="sell",
                ordType="limit", sz=f"{qty:.6f}",
                px=str(sell_price), tgtCcy="base_ccy"
            )
            if result.get("code") == "0":
                order_id = result["data"][0]["ordId"]
                log.info(f"📌 Maker賣單 ${sell_price:.2f} x {qty:.6f}｜#{order_id}")
                return True, order_id
            else:
                log.warning(f"掛賣單失敗({attempt+1})：{result.get('msg')}")
                time.sleep(2)
        except Exception as e:
            log.warning(f"掛賣單例外({attempt+1})：{e}")
            time.sleep(2)
    return False, None

def check_sell_filled(order_id, symbol, log):
    try:
        api_delay()
        d = tradeAPI.get_order(instId=symbol, ordId=order_id)["data"][0]
        return d.get("state", ""), float(d.get("avgPx") or 0), float(d.get("fillSz") or 0)
    except Exception as e:
        log.warning(f"查詢賣單失敗：{e}")
        return "unknown", 0.0, 0.0

def cancel_order(order_id, symbol, log):
    try:
        api_delay()
        tradeAPI.cancel_order(instId=symbol, ordId=order_id)
        log.info(f"❌ 取消訂單 #{order_id}")
    except Exception as e:
        log.warning(f"取消失敗：{e}")


# ══════════════════════════════════════════════════════════════════
#  賣出處理：拆成兩個獨立函式
#
#  check_sell_fills()    → 每輪必跑，不受 sell_ok 限制
#     1. 檢查所有賣單是否成交 → 結算利潤 + Discord 通知
#     2. 幫沒有賣單的批次補掛 → 買都買了當然要掛賣
#     3. API 回傳 unknown → 跳過等下輪，不做任何操作
#
#  manage_sell_orders()  → 只在 sell_ok 時跑
#     1. Timeout 重掛（超過 120 秒或價格偏移）
#     2. 重掛前先查狀態，避免取消已成交的訂單
#     3. 受 max_sell 限速，避免瘋狂重掛
# ══════════════════════════════════════════════════════════════════
def check_sell_fills(batches_file, layer, symbol, tick_size, best_ask, best_bid,
                     grid_spacing, inv_ratio, cfg,
                     stats_file, send_discord, log):
    """
    ✅ 每輪必跑，不受 sell_ok 限制
    檢查成交 + 補掛沒有賣單的批次
    回傳 filled_count
    """
    filled_count = 0
    all_b    = load_batches(batches_file, log=log)
    modified = False
    sell_spacing = get_sell_spacing(inv_ratio, grid_spacing, cfg["SKEW_SELL_SPACING"])

    for batch in all_b:
        if batch.get("layer") != layer:
            continue

        if batch.get("sell_order_id"):
            # ── 有賣單：檢查是否成交 ──
            state, fill_px, fill_sz = check_sell_filled(
                batch["sell_order_id"], symbol, log)

            if state == "filled" and fill_px > 0:
                profit = (fill_px * (1 - MAKER_FEE) - batch["buy_price"] * (1 + TAKER_FEE)) * batch["qty"]
                record_profit(profit, layer, stats_file)
                batch["_remove"] = True
                modified = True
                filled_count += 1
                log.info(f"✅ [{layer.upper()}] 批次#{batch['id']} ${fill_px:.2f}｜+${profit:.4f}")
                send_discord(
                    f"✅ **[{layer.upper()}] 止盈！批次#{batch['id']}**\n"
                    f"${batch['buy_price']:.2f} → ${fill_px:.2f}\n"
                    f"獲利：+${profit:.4f} USDT"
                )

            elif state in ("canceled", "failed"):
                batch["sell_order_id"]  = None
                batch["sell_placed_at"] = None
                modified = True

            elif state == "unknown":
                log.warning(f"⚠️ [{layer.upper()}] 批次#{batch['id']} API 回傳 unknown，跳過等下輪")
                continue

        # ── 沒賣單：補掛（不管 sell_ok，買都買了當然要掛賣）──
        if not batch.get("sell_order_id") and not batch.get("_remove"):
            sell_px = get_maker_sell_price(
                batch["buy_price"], sell_spacing, best_ask, best_bid, layer, tick_size, log)
            ok, oid = place_maker_sell(batch["qty"], sell_px, symbol, log)
            if ok:
                batch["sell_order_id"]  = oid
                batch["sell_price"]     = sell_px
                batch["sell_placed_at"] = time.time()
                modified = True
                log.info(f"📌 [{layer.upper()}] 批次#{batch['id']} 補掛賣單 ${sell_px:.2f}")

    if modified:
        save_batches([b for b in all_b if not b.get("_remove")], batches_file)

    return filled_count


def manage_sell_orders(batches_file, layer, grid_spacing, best_ask, best_bid,
                       inv_ratio, current_price, cfg, symbol, tick_size, log):
    """
    ✅ 只在 sell_ok 時執行：Timeout 重掛 + 價格偏移重掛
    重掛前先查狀態，避免取消已成交的訂單
    回傳 replace_count
    """
    max_sell      = cfg["INNER_MAX_SELL_LOOP"] if layer == "inner" else cfg["OUTER_MAX_SELL_LOOP"]
    replace_count = 0
    now           = time.time()
    sell_spacing  = get_sell_spacing(inv_ratio, grid_spacing, cfg["SKEW_SELL_SPACING"])

    all_b    = load_batches(batches_file, log=log)
    modified = False

    for batch in all_b:
        if batch.get("layer") != layer:
            continue
        if not batch.get("sell_order_id"):
            continue

        placed_at  = batch.get("sell_placed_at", now)
        time_out   = (now - placed_at) > MAKER_TIMEOUT_SECS
        price_move = (abs(current_price - batch.get("sell_price", current_price))
                      / current_price > PRICE_MOVE_TIMEOUT)

        if time_out or price_move:
            if replace_count >= max_sell:
                continue

            # ✅ 重掛前先查狀態，避免取消已成交的訂單
            state, _, _ = check_sell_filled(batch["sell_order_id"], symbol, log)
            if state in ("unknown", "filled"):
                continue

            reason = "逾時" if time_out else "價格偏移"
            log.info(f"🔄 [{layer.upper()}] 批次#{batch['id']} 重掛賣單（{reason}）")
            cancel_order(batch["sell_order_id"], symbol, log)
            batch["sell_order_id"]  = None
            batch["sell_placed_at"] = None
            modified = True

            # 立即重掛
            sell_px = get_maker_sell_price(
                batch["buy_price"], sell_spacing, best_ask, best_bid, layer, tick_size, log)
            ok, oid = place_maker_sell(batch["qty"], sell_px, symbol, log)
            if ok:
                batch["sell_order_id"]  = oid
                batch["sell_price"]     = sell_px
                batch["sell_placed_at"] = time.time()
            replace_count += 1

    if modified:
        save_batches(all_b, batches_file)

    return replace_count


# ══════════════════════════════════════════════════════════════════
#  主策略循環
# ══════════════════════════════════════════════════════════════════
def run_bot(cfg):
    symbol       = cfg["SYMBOL"]
    coin         = cfg["COIN"]
    tick_size    = cfg["TICK_SIZE"]
    batches_file = cfg["BATCHES_FILE"]
    stats_file   = cfg["STATS_FILE"]
    metrics_file = cfg["METRICS_FILE"]

    # ── Logger ──
    log = logging.getLogger(f"{coin}-BOT")
    log.propagate = False  # ✅ 防止 log 往 root logger 傳，避免每行印兩次
    if not log.handlers:
        handler_file   = logging.FileHandler(f"bot_{coin.lower()}.log", encoding="utf-8")
        handler_stream = logging.StreamHandler()
        formatter      = logging.Formatter(f"%(asctime)s [{coin}] [%(levelname)s] %(message)s")
        handler_file.setFormatter(formatter)
        handler_stream.setFormatter(formatter)
        log.addHandler(handler_file)
        log.addHandler(handler_stream)
        log.setLevel(logging.INFO)

    send_discord = make_discord_sender(symbol)

    # ── 啟動資訊 ──
    min_p = cfg.get("TRADE_MIN_PRICE")
    max_p = cfg.get("TRADE_MAX_PRICE")
    range_str = f"${min_p:.2f}~${max_p:.2f}" if min_p and max_p else "未設定"

    log.info("=" * 66)
    log.info(f"  🚀 Jane Street v16 {symbol} Bot 啟動！")
    log.info(f"  本金：${cfg['CAPITAL']:.0f}（$3300 × {cfg['ALLOCATION']*100:.0f}%）")
    log.info(f"  內層：${cfg['INNER_CAPITAL']:.0f} / {cfg['INNER_MAX_BATCH']}批 / 間距{cfg['INNER_BASE']*100}%")
    log.info(f"  外層：${cfg['OUTER_CAPITAL']:.0f} / {cfg['OUTER_MAX_BATCH']}批 / 間距{cfg['OUTER_BASE']*100}%")
    log.info(f"  費率：Taker{TAKER_FEE*100}% + Maker{MAKER_FEE*100}% = {(TAKER_FEE+MAKER_FEE)*100}%")
    log.info(f"  交易範圍：{range_str}｜倍率：{cfg.get('DYNAMIC_BUY_MIN_MULT','?')}x ~ {cfg.get('DYNAMIC_BUY_MAX_MULT','?')}x")
    log.info("=" * 66)

    send_discord(
        f"🚀 **{symbol} Bot v16 啟動！**\n"
        f"本金：${cfg['CAPITAL']:.0f} | 內{cfg['INNER_BASE']*100}% / 外{cfg['OUTER_BASE']*100}%\n"
        f"交易範圍：{range_str} | 倍率：{cfg.get('DYNAMIC_BUY_MIN_MULT','?')}x~{cfg.get('DYNAMIC_BUY_MAX_MULT','?')}x\n"
        f"File Lock ✅ | Maker Timeout ✅ | Dynamic Sizing ✅"
    )

    engine      = MarketEngine(cfg)
    crash_guard = CrashGuard(cfg, send_discord)
    buy_guard   = BuyRateGuard()
    of_cache    = OrderFlowCache(symbol)

    last_price        = get_market_price(symbol, log)
    last_candle_time  = 0
    last_report_time  = 0
    usdt_balance, coin_balance = get_balances(coin, log)
    loop_counter = 0
    inv_ratio    = 0.0

    last_buy_inner = last_price
    last_buy_outer = last_price
    log.info(f"  錨點初始化：${last_price:.2f}")

    maker_placed_today = 0
    maker_filled_today = 0
    fees_today         = 0.0
    last_report_date   = str(date.today())

    while True:
        try:
            current_price = get_market_price(symbol, log)
            now           = time.time()

            crash_guard.add_price(current_price)

            if now - last_candle_time >= 900:
                high, low, close = get_candle(symbol, log)
                if high > 0:
                    engine.update(high, low, close)
                    last_candle_time = now

            if loop_counter % BALANCE_INTERVAL == 0:
                usdt_balance, coin_balance = get_balances(coin, log)

            of_cache.update(log)
            obi, tfi = of_cache.obi, of_cache.tfi

            trend, ema_f, ema_s = engine.get_trend()
            is_crash             = crash_guard.check(current_price, obi, tfi, log)
            regime               = engine.get_vol_regime(current_price)
            inner_spacing        = engine.get_inner_spacing(current_price, is_crash, trend)
            outer_spacing        = engine.get_outer_spacing(is_crash, trend)
            inv_ratio            = get_inv(usdt_balance, coin_balance, current_price)
            mid_price, best_bid, best_ask = get_mid_price(symbol, tick_size, log)

            # ── Anchor Reset ──
            inner_reset = inner_spacing * cfg["INNER_RESET_MULT"]
            outer_reset = outer_spacing * cfg["OUTER_RESET_MULT"]
            if last_buy_inner > 0 and (current_price - last_buy_inner) / last_buy_inner > inner_reset:
                last_buy_inner = current_price
                log.info(f"🔄 內層錨點重設（偏離>{inner_reset*100:.2f}%）→ ${last_buy_inner:.2f}")
            if last_buy_outer > 0 and (current_price - last_buy_outer) / last_buy_outer > outer_reset:
                last_buy_outer = current_price
                log.info(f"🔄 外層錨點重設（偏離>{outer_reset*100:.2f}%）→ ${last_buy_outer:.2f}")

            inner_trigger, _ = get_buy_trigger(last_price, mid_price, last_buy_inner, inner_spacing)
            outer_trigger, _ = get_buy_trigger(last_price, mid_price, last_buy_outer, outer_spacing)

            all_batches   = load_batches(batches_file, log=log)
            inner_batches = [b for b in all_batches if b.get("layer") == "inner"]
            outer_batches = [b for b in all_batches if b.get("layer") == "outer"]

            # ✅ v16：calc_buy_amount 傳入 current_price
            inner_buy_amount = calc_buy_amount("inner", len(inner_batches), cfg, current_price)
            outer_buy_amount = calc_buy_amount("outer", len(outer_batches), cfg, current_price)

            # ✅ v16：buy_ok 加入價格邊界檢查
            in_range = is_price_in_range(cfg, current_price)
            buy_ok   = not is_crash and inv_ratio < MAX_INVENTORY and buy_guard.can_buy() and in_range
            sell_ok  = inv_ratio > MIN_INVENTORY

            # ── 每小時日報 + 跨日重置 ──
            if now - last_report_time >= 3600:
                p, t, pi, po, tp = get_today_stats(stats_file)
                fill_rate = update_metrics(t, fees_today, p, inv_ratio,
                                           maker_placed_today, maker_filled_today,
                                           metrics_file, symbol)
                target = cfg["DAILY_TARGET"]
                progress = min(p / target * 100, 100) if target > 0 else 0
                bar = "█" * int(progress / 10) + "░" * (10 - int(progress / 10))
                send_discord(
                    f"📊 **{symbol} 日報**\n"
                    f"今日淨利：+${p:.4f} USDT\n"
                    f"進度：[{bar}] {progress:.1f}%\n"
                    f"交易：{t}筆 | Maker成交率：{fill_rate:.1f}%\n"
                    f"庫存：{inv_ratio*100:.1f}% {coin}"
                )
                log.info(f"📈 日報｜+${p:.4f}({t}筆) Fill:{fill_rate:.1f}%")
                last_report_time = now

                if str(date.today()) != last_report_date:
                    maker_placed_today = 0
                    maker_filled_today = 0
                    fees_today         = 0.0
                    last_report_date   = str(date.today())
                    log.info("🗓️  跨日重置計數器")

            # ── 狀態顯示 ──
            multiplier = get_dynamic_multiplier(cfg, current_price)
            skew_str   = f"⚡Skew" if inv_ratio > INVENTORY_SKEW_THRESHOLD else ""
            crash_str  = f"🚨崩盤{int(crash_guard.cooldown_until-now)}s" if is_crash else "✅"
            regime_str = {"low": "🟢低波", "normal": "🟡中波", "high": "🔴高波"}.get(regime, "")
            range_ok   = "✅範圍內" if in_range else "🚫超出範圍"
            inner_dist = (current_price - inner_trigger) / current_price * 100
            outer_dist = (current_price - outer_trigger) / current_price * 100
            log.info(
                f"\n{'─'*66}\n"
                f"  💰 USDT:${usdt_balance:.2f}  {coin}:{coin_balance:.6f}  庫存:{inv_ratio*100:.1f}% {skew_str}\n"
                f"  📊 現價:${current_price:.2f}  {regime_str}  {'📉下跌' if trend=='down' else '📈正常'}  {crash_str}\n"
                f"  🎯 倍率:{multiplier:.2f}x  {range_ok}  範圍:${cfg.get('TRADE_MIN_PRICE',0):.0f}~${cfg.get('TRADE_MAX_PRICE',0):.0f}\n"
                f"  🔵 內層 間距:{inner_spacing*100:.2f}% 觸發:${inner_trigger:.2f} 距離:{inner_dist:.2f}% "
                f"批次:{len(inner_batches)}/{cfg['INNER_MAX_BATCH']} 每批:${inner_buy_amount:.2f}\n"
                f"  🟠 外層 間距:{outer_spacing*100:.2f}% 觸發:${outer_trigger:.2f} 距離:{outer_dist:.2f}% "
                f"批次:{len(outer_batches)}/{cfg['OUTER_MAX_BATCH']} 每批:${outer_buy_amount:.2f}\n"
                f"  📖 OBI:{obi:.2f} TFI:{tfi:.2f}  "
                f"{'✅買' if buy_ok else '🚫買'}  {'✅賣' if sell_ok else '🚫賣'}\n"
                f"{'─'*66}"
            )

            # ══ 成交檢查 + 補掛（永遠執行，不受庫存限制）══
            fc_inner = check_sell_fills(
                batches_file, "inner", symbol, tick_size, best_ask, best_bid,
                inner_spacing, inv_ratio, cfg,
                stats_file, send_discord, log)
            fc_outer = check_sell_fills(
                batches_file, "outer", symbol, tick_size, best_ask, best_bid,
                outer_spacing, inv_ratio, cfg,
                stats_file, send_discord, log)
            maker_filled_today += fc_inner + fc_outer

            # ══ 賣單管理：Timeout 重掛（只在庫存足夠時）══
            if sell_ok:
                manage_sell_orders(
                    batches_file, "inner", inner_spacing, best_ask, best_bid,
                    inv_ratio, current_price, cfg, symbol, tick_size, log)
                manage_sell_orders(
                    batches_file, "outer", outer_spacing, best_ask, best_bid,
                    inv_ratio, current_price, cfg, symbol, tick_size, log)

            # ══ 內層買入 ══
            if buy_ok and current_price <= inner_trigger:
                if len(inner_batches) >= cfg["INNER_MAX_BATCH"]:
                    log.warning("⚠️  內層已達最大批次")
                else:
                    lock = acquire_usdt_lock()
                    if lock is None:
                        log.warning("⚠️  無法取得 USDT 鎖，跳過此輪買入")
                    else:
                        try:
                            usdt_balance, coin_balance = get_balances(coin, log)
                            _, pool_remaining = calc_pool_spent(batches_file, "inner", cfg)
                            safe_amount = min(inner_buy_amount, usdt_balance, pool_remaining)
                            if safe_amount < MIN_BUY_AMOUNT:
                                log.warning(f"⚠️  內層額度不足（pool剩:${pool_remaining:.2f} USDT:${usdt_balance:.2f}）")
                            else:
                                log.info(f"📉 [內層] 買入 ${current_price:.2f} ≤ ${inner_trigger:.2f}｜${safe_amount:.2f}（{multiplier:.2f}x）")
                                success, fill_price, fill_sz = execute_taker_buy(safe_amount, symbol, log, send_discord)
                                if success:
                                    sell_spacing = get_sell_spacing(inv_ratio, inner_spacing, cfg["SKEW_SELL_SPACING"])
                                    sell_px      = get_maker_sell_price(fill_price, sell_spacing, best_ask, best_bid, "inner", tick_size, log)
                                    s_ok, s_id   = place_maker_sell(fill_sz, sell_px, symbol, log)
                                    add_batch(fill_price, fill_sz, safe_amount, "inner",
                                              symbol, batches_file, log,
                                              sell_order_id=s_id if s_ok else None,
                                              sell_price=sell_px if s_ok else None)
                                    last_price     = fill_price
                                    last_buy_inner = fill_price
                                    buy_guard.record_buy()
                                    fees_today        += fill_price * fill_sz * TAKER_FEE
                                    maker_placed_today += 1
                                    usdt_balance, coin_balance = get_balances(coin, log)
                                    log.info(f"🔄 [內層] 錨點→${last_buy_inner:.2f}｜下次:${fill_price*(1-inner_spacing):.2f}")
                                    send_discord(
                                        f"📉 **[內層] 買入！**（{multiplier:.1f}x）\n"
                                        f"${fill_price:.2f} x {fill_sz:.6f} {coin}\n"
                                        f"花費：${safe_amount:.2f} | 賣單：${sell_px:.2f}"
                                    )
                        finally:
                            release_usdt_lock(lock)

            # ══ 外層買入 ══
            if buy_ok and current_price <= outer_trigger:
                if len(outer_batches) >= cfg["OUTER_MAX_BATCH"]:
                    log.warning("⚠️  外層已達最大批次")
                else:
                    lock = acquire_usdt_lock()
                    if lock is None:
                        log.warning("⚠️  無法取得 USDT 鎖，跳過此輪買入")
                    else:
                        try:
                            usdt_balance, coin_balance = get_balances(coin, log)
                            _, pool_remaining = calc_pool_spent(batches_file, "outer", cfg)
                            safe_amount = min(outer_buy_amount, usdt_balance, pool_remaining)
                            if safe_amount < MIN_BUY_AMOUNT:
                                log.warning(f"⚠️  外層額度不足（pool剩:${pool_remaining:.2f} USDT:${usdt_balance:.2f}）")
                            else:
                                log.info(f"📉 [外層] 買入 ${current_price:.2f} ≤ ${outer_trigger:.2f}｜${safe_amount:.2f}（{multiplier:.2f}x）")
                                success, fill_price, fill_sz = execute_taker_buy(safe_amount, symbol, log, send_discord)
                                if success:
                                    sell_spacing = get_sell_spacing(inv_ratio, outer_spacing, cfg["SKEW_SELL_SPACING"])
                                    sell_px      = get_maker_sell_price(fill_price, sell_spacing, best_ask, best_bid, "outer", tick_size, log)
                                    s_ok, s_id   = place_maker_sell(fill_sz, sell_px, symbol, log)
                                    add_batch(fill_price, fill_sz, safe_amount, "outer",
                                              symbol, batches_file, log,
                                              sell_order_id=s_id if s_ok else None,
                                              sell_price=sell_px if s_ok else None)
                                    last_price     = fill_price
                                    last_buy_outer = fill_price
                                    buy_guard.record_buy()
                                    fees_today        += fill_price * fill_sz * TAKER_FEE
                                    maker_placed_today += 1
                                    usdt_balance, coin_balance = get_balances(coin, log)
                                    log.info(f"🔄 [外層] 錨點→${last_buy_outer:.2f}｜下次:${fill_price*(1-outer_spacing):.2f}")
                                    send_discord(
                                        f"📉 **[外層] 買入！**（{multiplier:.1f}x）\n"
                                        f"${fill_price:.2f} x {fill_sz:.6f} {coin}\n"
                                        f"花費：${safe_amount:.2f} | 賣單：${sell_px:.2f}"
                                    )
                        finally:
                            release_usdt_lock(lock)

            loop_counter += 1
            time.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            p, t, pi, po, tp = get_today_stats(stats_file)
            fill_rate = update_metrics(t, fees_today, p, inv_ratio,
                                       maker_placed_today, maker_filled_today,
                                       metrics_file, symbol)
            send_discord(f"⛔ **{symbol} Bot 已停止**｜今日+${p:.4f}({t}筆)")
            log.info(f"⛔ {symbol} Bot 停止")
            break
        except RuntimeError as e:
            log.error(f"嚴重錯誤：{e}")
            send_discord(f"🆘 **{symbol} Bot 嚴重錯誤！**\n{e}")
            time.sleep(30)
        except Exception as e:
            log.error(f"未預期錯誤：{e}")
            time.sleep(5)