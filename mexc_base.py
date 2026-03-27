"""
╔══════════════════════════════════════════════════════════════════╗
║         Jane Street v16-MEXC — Bot Base（MEXC 版共用邏輯）       ║
║                                                                  ║
║  架構說明：                                                      ║
║    mexc_base.py  → MEXC 版策略邏輯                               ║
║    mexc_eth.py   → ETH 參數設定 + 啟動入口                       ║
║    mexc_sol.py   → SOL 參數設定 + 啟動入口                       ║
║                                                                  ║
║  與 OKX 版差異：                                                 ║
║    ✅ API 改用 pymexc（MEXC Spot V3）                            ║
║    ✅ Maker 0% / Taker 0.05%（成本 0.05% vs OKX 0.18%）         ║
║    ✅ 間距縮小：ETH 0.22% / SOL 0.27%（觸發更頻繁）             ║
║    ✅ Symbol 格式：ETHUSDT（無連字號）                            ║
║    ✅ 成交檢查與重掛分離（check_sell_fills / manage_sell_orders） ║
║    ✅ save_batches 空值保護 / log.propagate = False               ║
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
from pymexc import spot

load_dotenv()

MEXC_API_KEY    = os.environ.get("MEXC_API_KEY")
MEXC_SECRET_KEY = os.environ.get("MEXC_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("MEXC_DISCORD_WEBHOOK_URL")

# ── MEXC 手續費（Maker 0% + Taker 0.05%）──
TAKER_FEE           = 0.0005    # 0.05%
MAKER_FEE           = 0.0       # 0%
LOOP_INTERVAL       = 2
MAX_RETRIES         = 3
FILL_WAIT_SECS      = 2
BALANCE_INTERVAL    = 10
MIN_BUY_AMOUNT      = 6.0       # MEXC 最小下單量較低
API_CALL_DELAY      = 0.05      # 降低延遲提升反應速度
OB_DEPTH            = 20
TFI_LIMIT           = 50
OBI_REFRESH_SECS    = 10
MAX_BUY_10S         = 4
MAKER_TIMEOUT_SECS  = 120
PRICE_MOVE_TIMEOUT  = 0.005     # 0.5%
MAX_INVENTORY       = 0.99
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

# ── MEXC API Client ──
mexc_client = spot.HTTP(api_key=MEXC_API_KEY, api_secret=MEXC_SECRET_KEY)

# ✅ API Timeout Patch：避免單一 API call 卡死整輪
_orig_request = mexc_client.session.request
def _patched_request(method, url, *args, **kwargs):
    if "timeout" not in kwargs or kwargs["timeout"] is None:
        kwargs["timeout"] = (0.8, 2.5)  # (connect, read)
    return _orig_request(method, url, *args, **kwargs)
mexc_client.session.request = _patched_request

USDT_LOCK_FILE = "/tmp/trading_bot_mexc_usdt.lock"

def api_delay():
    time.sleep(API_CALL_DELAY)


# ══════════════════════════════════════════════════════════════════
#  啟動時價格範圍輸入
# ══════════════════════════════════════════════════════════════════
def ask_price_range(config):
    symbol = config["SYMBOL"]
    coin   = config["COIN"]

    print(f"\n  ┌──────────────────────────────────────┐")
    print(f"  │  {symbol} 動態買入倍率設定 (MEXC)     │")
    print(f"  │                                        │")
    print(f"  │  越接近下限 → 買入量最多 {config['DYNAMIC_BUY_MAX_MULT']}x              │")
    print(f"  │  越接近上限 → 買入量最少 {config['DYNAMIC_BUY_MIN_MULT']}x              │")
    print(f"  │  超出範圍 → 停止買入                   │")
    print(f"  └──────────────────────────────────────┘\n")

    # 取得現價
    try:
        ticker = mexc_client.ticker_price(symbol=symbol)
        current = float(ticker.get("price", 0))
        if current > 0:
            print(f"  📊 {coin} 現價：${current:.2f}\n")
    except Exception:
        current = 0

    while True:
        try:
            min_input = input(f"  輸入 {coin} 價格下限（$）：").strip()
            min_price = float(min_input)
            max_input = input(f"  輸入 {coin} 價格上限（$）：").strip()
            max_price = float(max_input)

            if min_price >= max_price:
                print("  ❌ 下限必須小於上限，請重新輸入\n")
                continue

            if current > 0:
                if current < min_price or current > max_price:
                    print(f"  ⚠️  注意：現價 ${current:.2f} 不在範圍內！\n")

            print(f"\n  ✅ 設定完成：${min_price:.2f} ~ ${max_price:.2f}")
            config["TRADE_MIN_PRICE"] = min_price
            config["TRADE_MAX_PRICE"] = max_price
            break
        except ValueError:
            print("  ❌ 請輸入有效數字\n")


def auto_detect_capital(config):
    """
    ✅ 啟動時自動偵測帳戶 USDT 餘額，計算本金分配
    不再需要手動寫死 CAPITAL
    """
    try:
        info = mexc_client.account_information()
        usdt = 0.0
        for asset in info.get("balances", []):
            if asset["asset"] == "USDT":
                usdt = float(asset["free"]) + float(asset.get("locked", 0))
                break

        if usdt <= 0:
            print("  ⚠️  帳戶沒有 USDT，使用預設 CAPITAL")
            return

        alloc = config["ALLOCATION"]
        capital = usdt * alloc
        config["CAPITAL"]       = capital
        config["INNER_CAPITAL"] = capital * 0.0
        config["OUTER_CAPITAL"] = capital * 1.0

        print(f"  💰 偵測到 USDT 餘額：${usdt:.2f}")
        print(f"  📊 {config['COIN']} 分配 {alloc*100:.0f}% = ${capital:.2f}")
        print(f"     外層：${capital * 1.0:.2f}")

        print()

    except Exception as e:
        print(f"  ⚠️  無法偵測餘額：{e}，使用預設 CAPITAL")


def update_capital(cfg, coin, current_price, log):
    """
    ✅ 每小時更新一次 CAPITAL
    用 USDT + 幣的市值 算總資產，不只看 USDT
    這樣不管資金是在 USDT 還是在幣裡，CAPITAL 都穩定
    """
    try:
        info = mexc_client.account_information()
        usdt_total = 0.0
        coin_total = 0.0
        for asset in info.get("balances", []):
            if asset["asset"] == "USDT":
                usdt_total = float(asset.get("free", 0)) + float(asset.get("locked", 0))
            elif asset["asset"] == coin:
                coin_total = float(asset.get("free", 0)) + float(asset.get("locked", 0))

        # 總資產 = USDT + 幣 × 現價
        total_asset = usdt_total + coin_total * current_price

        if total_asset <= 0:
            return

        alloc   = cfg["ALLOCATION"]
        old_cap = cfg["CAPITAL"]
        new_cap = total_asset * alloc

        cfg["CAPITAL"]       = new_cap
        cfg["INNER_CAPITAL"] = new_cap * 0.0
        cfg["OUTER_CAPITAL"] = new_cap * 1.0

        change = ((new_cap - old_cap) / old_cap * 100) if old_cap > 0 else 0
        log.info(
            f"💰 CAPITAL 更新｜總資產:${total_asset:.2f} "
            f"→ {coin} 分配:${new_cap:.2f}（{change:+.1f}%）"
            f"｜內層每批:${new_cap * 0.70 / cfg['INNER_MAX_BATCH']:.2f}" if cfg["INNER_MAX_BATCH"] > 0 else ""
        )

    except Exception as e:
        log.warning(f"⚠️ CAPITAL 更新失敗：{e}")


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
                json={"content": f"[MEXC-{symbol}] {msg}"},
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
        self.cfg    = cfg
        self.closes = deque(maxlen=EMA_SLOW + 1)
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
        elif regime == "high" and self.atr:
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
            data    = mexc_client.order_book(symbol=self.symbol, limit=OB_DEPTH)
            bid_vol = sum(float(b[1]) for b in data.get("bids", []))
            ask_vol = sum(float(a[1]) for a in data.get("asks", []))
            total   = bid_vol + ask_vol
            if total > 0:
                self.obi = bid_vol / total
        except Exception as e:
            log.warning(f"OBI 更新失敗：{e}")
        try:
            api_delay()
            trades = mexc_client.agg_trades(symbol=self.symbol, limit=TFI_LIMIT)
            if isinstance(trades, list):
                # MEXC agg_trades: m=true → buyer is maker → 賣方主動 (sell)
                #                  m=false → seller is maker → 買方主動 (buy)
                buy_v  = sum(float(t.get("q", 0)) for t in trades if not t.get("m", True))
                sell_v = sum(float(t.get("q", 0)) for t in trades if t.get("m", False))
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
        data     = mexc_client.order_book(symbol=symbol, limit=5)
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
    # MEXC maker 0%，最低利潤只需 cover taker fee
    min_profit_price = buy_price * (1 + TAKER_FEE + 0.0002)
    # ✅ 賣價必須高於現在市場 ask，避免 LIMIT_MAKER 被拒絕
    min_market_price = (best_ask + tick_size) if best_ask > 0 else min_profit_price
    min_price = max(min_profit_price, min_market_price)

    # 根據 tick_size 決定 round 精度（0.01→2, 0.001→3, 0.0001→4）
    import math
    decimals = max(0, -int(math.log10(tick_size)))

    if layer == "outer":
        return round(max(buy_price * (1 + sell_spacing), min_price), decimals)

    try:
        if best_bid > 0 and best_ask > 0:
            spread = best_ask - best_bid
            if spread > 2 * tick_size:
                micro_price = best_bid + spread * 0.6
                if micro_price >= min_price:
                    return round(micro_price, decimals)
            maker_price = best_ask - tick_size
            if maker_price >= min_price:
                return round(maker_price, decimals)
    except Exception as e:
        log.warning(f"Microspread 計算失敗：{e}")

    return round(max(buy_price * (1 + sell_spacing), min_price), decimals)


# ══════════════════════════════════════════════════════════════════
#  批次管理
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
        return
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


TRADES_LOG_FILE = os.path.expanduser("~/AI-trading-bot/trades_log.json")
TRADES_LOG_MAX = 500

def _log_trade(symbol, layer, batch_id, buy_price, sell_price, profit):
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "layer": layer.upper(),
        "batch": batch_id,
        "buy_price": round(buy_price, 6),
        "sell_price": round(sell_price, 6),
        "profit": round(profit, 6),
    }
    try:
        try:
            with open(TRADES_LOG_FILE) as f:
                trades = json.load(f)
        except:
            trades = []
        trades.append(entry)
        if len(trades) > TRADES_LOG_MAX:
            trades = trades[-TRADES_LOG_MAX:]
        with open(TRADES_LOG_FILE, "w") as f:
            json.dump(trades, f)
    except:
        pass
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
#  市場資料 & 帳戶（MEXC API）
# ══════════════════════════════════════════════════════════════════
def get_market_price(symbol, log):
    for i in range(MAX_RETRIES):
        try:
            api_delay()
            ticker = mexc_client.ticker_price(symbol=symbol)
            return float(ticker["price"])
        except Exception as e:
            log.warning(f"價格失敗({i+1})：{e}")
            time.sleep(1)
    raise RuntimeError(f"[{symbol}] 無法取得市場價格")

def get_candle(symbol, log):
    for i in range(MAX_RETRIES):
        try:
            api_delay()
            klines = mexc_client.klines(symbol=symbol, interval="15m", limit=2)
            if klines and len(klines) >= 2:
                # MEXC klines 舊到新排序，[-2] 是上一根完整的 K 線
                c = klines[-2]
                # MEXC kline format: [openTime, open, high, low, close, volume, ...]
                return float(c[2]), float(c[3]), float(c[4])
        except Exception as e:
            log.warning(f"K線失敗({i+1})：{e}")
            time.sleep(1)
    return 0.0, 0.0, 0.0

def get_balances(coin, log):
    try:
        api_delay()
        info = mexc_client.account_information()
        usdt, coin_bal = 0.0, 0.0
        coin_free = 0.0
        for asset in info.get("balances", []):
            if asset["asset"] == "USDT":
                usdt = float(asset.get("free", 0))
            elif asset["asset"] == coin:
                coin_free = float(asset.get("free", 0))
                # ✅ free + locked，含凍結在賣單裡的幣，避免庫存算 0%
                coin_bal = coin_free + float(asset.get("locked", 0))
        return usdt, coin_bal, coin_free
    except Exception as e:
        log.error(f"餘額失敗：{e}")
        return 0.0, 0.0

def get_inv(usdt, coin_bal, price):
    total = coin_bal * price + usdt
    return (coin_bal * price / total) if total > 0 else 0.5


# ══════════════════════════════════════════════════════════════════
#  動態買入倍率
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
#  三區資金配置：高位 / 中位 / 低位
# ══════════════════════════════════════════════════════════════════
def get_price_zone(cfg, current_price):
    """回傳 'high' / 'mid' / 'low'"""
    min_p = cfg.get("TRADE_MIN_PRICE", 0)
    max_p = cfg.get("TRADE_MAX_PRICE", 999999)
    span  = max_p - min_p
    if span <= 0:
        return "mid"
    high_threshold = max_p - span * 0.15   # 上限前 15%
    low_threshold  = min_p + span * 0.25   # 下限後 25%
    if current_price >= high_threshold:
        return "high"
    elif current_price <= low_threshold:
        return "low"
    else:
        return "mid"

def get_zone_batch_mult(zone, is_crash=False):
    """A：三區 batch 倍率"""
    if zone == "high":
        return 0.3
    elif zone == "low":
        return 1.2 if is_crash else 1.8
    else:  # mid
        return 1.0

def get_zone_max_inventory(zone):
    """B：三區 MAX_INVENTORY"""
    if zone == "high":
        return 0.40
    elif zone == "low":
        return 0.78
    else:  # mid
        return 0.60

def get_zone_max_batch(zone, base_max_batch):
    """D：三區批次上限"""
    if base_max_batch <= 0:
        return 0
    if zone == "high":
        return max(int(base_max_batch * 0.35), 5)   # 高位只用 35% 批次
    elif zone == "low":
        return base_max_batch                         # 低位全開
    else:  # mid
        return max(int(base_max_batch * 0.70), 5)    # 中位用 70%

def calc_buy_amount(layer, count, cfg, current_price, is_crash=False):
    capital   = cfg["INNER_CAPITAL"] if layer == "inner" else cfg["OUTER_CAPITAL"]
    max_batch = cfg["INNER_MAX_BATCH"] if layer == "inner" else cfg["OUTER_MAX_BATCH"]
    remaining = max_batch - count
    if remaining <= 0:
        return MIN_BUY_AMOUNT
    base_amount = capital / remaining
    # A：三區 batch 倍率（取代舊線性倍率）
    zone        = get_price_zone(cfg, current_price)
    multiplier  = get_zone_batch_mult(zone, is_crash)
    final_amount = base_amount * multiplier
    return max(final_amount, MIN_BUY_AMOUNT)

def get_dynamic_multiplier(cfg, current_price):
    min_p    = cfg.get("TRADE_MIN_PRICE")
    max_p    = cfg.get("TRADE_MAX_PRICE")
    min_mult = cfg.get("DYNAMIC_BUY_MIN_MULT", 0.5)
    max_mult = cfg.get("DYNAMIC_BUY_MAX_MULT", 2.5)

    if min_p and max_p and max_p > min_p and min_p < current_price < max_p:
        price_ratio = (current_price - min_p) / (max_p - min_p)
        return max_mult - price_ratio * (max_mult - min_mult)
    return 1.0


def get_dynamic_reset_mult(cfg, current_price):
    """
    ✅ 動態錨點重設倍率
    接近下限 → 小（1.2）→ 快速跟價，積極買入（便宜不怕套）
    接近上限 → 大（3.0）→ 慢慢跟，避免套在高點
    用跟動態倍率一樣的 price_ratio 計算
    """
    min_p = cfg.get("TRADE_MIN_PRICE")
    max_p = cfg.get("TRADE_MAX_PRICE")
    RESET_MIN = 2.0   # 接近下限：漲 0.30% 就重設（2.0 × 0.15%）
    RESET_MAX = 4.0   # 接近上限：漲 0.60% 才重設（4.0 × 0.15%）

    if min_p and max_p and max_p > min_p and min_p < current_price < max_p:
        price_ratio = (current_price - min_p) / (max_p - min_p)
        # 接近下限 ratio→0 → RESET_MIN；接近上限 ratio→1 → RESET_MAX
        return RESET_MIN + price_ratio * (RESET_MAX - RESET_MIN)
    return cfg.get("INNER_RESET_MULT", 2.5)  # fallback

def is_price_in_range(cfg, current_price):
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
#  執行訂單（MEXC API）
# ══════════════════════════════════════════════════════════════════
def execute_taker_buy(buy_amount, symbol, log, send_discord):
    """MEXC 市價買入：用 quote_order_qty 指定 USDT 金額"""
    for attempt in range(MAX_RETRIES):
        try:
            api_delay()
            result = mexc_client.order(
                symbol=symbol,
                side="BUY",
                order_type="MARKET",
                quote_order_qty=str(round(buy_amount, 2))
            )
            if result.get("orderId"):
                order_id = result["orderId"]
                time.sleep(FILL_WAIT_SECS)
                for _ in range(MAX_RETRIES):
                    api_delay()
                    d = mexc_client.query_order(symbol=symbol, order_id=order_id)
                    fill_sz   = float(d.get("executedQty") or 0)
                    cum_quote = float(d.get("cummulativeQuoteQty") or 0)
                    status    = d.get("status", "")
                    if status == "FILLED" and fill_sz > 0 and cum_quote > 0:
                        avg_px = cum_quote / fill_sz
                        log.info(f"✅ 買入 ${avg_px:.2f} x {fill_sz:.6f}")
                        return True, avg_px, fill_sz
                    time.sleep(FILL_WAIT_SECS)
                # 幽靈倉位警告
                warn_msg = (
                    f"⚠️ **幽靈倉位警告！**\n"
                    f"訂單 #{order_id} 已送出但查詢逾時\n"
                    f"請手動到 MEXC 確認是否成交"
                )
                log.warning(f"⚠️ 幽靈倉位警告！訂單#{order_id} 已送出但查詢逾時")
                send_discord(warn_msg)
                return False, 0.0, 0.0
            else:
                msg = result.get("msg", result)
                log.warning(f"買入失敗({attempt+1})：{msg}")
                time.sleep(2)
        except Exception as e:
            log.warning(f"買入例外({attempt+1})：{e}")
            time.sleep(2)
    return False, 0.0, 0.0

def place_maker_sell(qty, sell_price, symbol, log):
    """MEXC 限價賣單（LIMIT_MAKER 確保 maker，如果會被吃則拒絕）"""

    for attempt in range(MAX_RETRIES):
        try:
            api_delay()
            result = mexc_client.order(
                symbol=symbol,
                side="SELL",
                order_type="LIMIT_MAKER",
                quantity=f"{qty:.6f}",
                price=str(sell_price)
            )
            if result.get("orderId"):
                order_id = result["orderId"]
                log.info(f"📌 Maker賣單 ${sell_price} x {qty:.6f}｜#{order_id}")
                return True, order_id
            else:
                msg = result.get("msg", result)
                log.warning(f"掛賣單失敗({attempt+1})：{msg}")
                time.sleep(2)
        except Exception as e:
            log.warning(f"掛賣單例外({attempt+1})：{e}")
            time.sleep(2)
    return False, None

def check_sell_filled(order_id, symbol, log):
    """查詢賣單狀態 — MEXC 沒有 avgPrice，要從 cummulativeQuoteQty/executedQty 算"""
    try:
        api_delay()
        d = mexc_client.query_order(symbol=symbol, order_id=order_id)
        status = d.get("status", "")

        if status == "FILLED":
            fill_sz   = float(d.get("executedQty") or 0)
            cum_quote = float(d.get("cummulativeQuoteQty") or 0)
            avg_px    = cum_quote / fill_sz if fill_sz > 0 else 0.0
            return "filled", avg_px, fill_sz
        elif status in ("CANCELED", "PARTIALLY_CANCELED"):
            return "canceled", 0.0, 0.0
        elif status in ("NEW", "PARTIALLY_FILLED"):
            return "live", 0.0, 0.0
        else:
            return "unknown", 0.0, 0.0
    except Exception as e:
        log.warning(f"查詢賣單失敗：{e}")
        return "unknown", 0.0, 0.0

def cancel_order(order_id, symbol, log):
    try:
        api_delay()
        mexc_client.cancel_order(symbol=symbol, order_id=order_id)
        log.info(f"❌ 取消訂單 #{order_id}")
    except Exception as e:
        log.warning(f"取消失敗：{e}")


# ══════════════════════════════════════════════════════════════════
#  賣出處理：兩個獨立函式
# ══════════════════════════════════════════════════════════════════
# ✅ 全域查詢時間記錄，避免每輪重新載入 JSON 時冷卻失效
_sell_check_times = {}

def check_sell_fills(batches_file, layer, symbol, tick_size, best_ask, best_bid,
                     grid_spacing, inv_ratio, cfg,
                     stats_file, send_discord, log, free_coin=999999.0):
    """✅ 每輪必跑，不受 sell_ok 限制"""
    filled_count = 0

    all_b    = load_batches(batches_file, log=log)
    modified = False
    sell_spacing = get_sell_spacing(inv_ratio, grid_spacing, cfg["SKEW_SELL_SPACING"])


    for batch in all_b:
        if batch.get("layer") != layer:
            continue

        if batch.get("sell_order_id"):
            now_t = time.time()
            placed_at = batch.get("sell_placed_at", 0) or 0
            batch_key = f"{layer}_{batch['id']}"
            last_checked = _sell_check_times.get(batch_key, 0)
            # ✅ 掛單後 10 秒才開始查，之後每 5 秒查一次
            if now_t - placed_at < 60:
                continue
            if now_t - last_checked < 60:
                continue
            _sell_check_times[batch_key] = now_t
            state, fill_px, fill_sz = check_sell_filled(
                batch["sell_order_id"], symbol, log)

            # ✅ Debug：每次查詢都印出狀態（確認 check_sell_fills 有在跑）
            if state != "live":
                log.info(f"🔍 [{layer.upper()}] 批次#{batch['id']} 查詢狀態={state} px={fill_px}")

            if state == "filled" and fill_px > 0:
                profit = (fill_px * (1 - MAKER_FEE) - batch["buy_price"] * (1 + TAKER_FEE)) * batch["qty"]
                record_profit(profit, layer, stats_file)
                _log_trade(symbol, layer, batch["id"], batch["buy_price"], fill_px, profit)
                batch["_remove"] = True
                modified = True
                filled_count += 1
                log.info(f"✅ [{layer.upper()}] 批次#{batch['id']} ${fill_px:.2f}｜+${profit:.4f}")
                send_discord(
                    f"✅ **[{layer.upper()}] 止盈！批次#{batch['id']}**\n"
                    f"${batch['buy_price']:.2f} → ${fill_px:.2f}\n"
                    f"獲利：+${profit:.4f} USDT"
                )

            elif state == "canceled":
                batch["sell_order_id"]  = None
                batch["sell_placed_at"] = None
                modified = True

            elif state == "unknown":
                log.warning(f"⚠️ [{layer.upper()}] 批次#{batch['id']} API 回傳 unknown，跳過等下輪")
                continue

        if not batch.get("sell_order_id") and not batch.get("_remove"):
            if free_coin < batch["qty"]:
                continue
            sell_px = get_maker_sell_price(
                batch["buy_price"], sell_spacing, best_ask, best_bid, layer, tick_size, log)
            ok, oid = place_maker_sell(batch["qty"], sell_px, symbol, log)
            if ok:
                free_coin -= batch["qty"]  # ✅ 成功掛單後扣除 free_coin，避免超賣
                batch["sell_order_id"]  = oid
                batch["sell_price"]     = sell_px
                batch["sell_placed_at"] = time.time()
                modified = True
                log.info(f"📌 [{layer.upper()}] 批次#{batch['id']} 補掛賣單 ${sell_px:.2f}")

    if modified:
        save_batches([b for b in all_b if not b.get("_remove")], batches_file)

    return filled_count


def manage_sell_orders(batches_file, layer, grid_spacing, best_ask, best_bid,
                       inv_ratio, current_price, cfg, symbol, tick_size, log, free_coin=999999.0):
    """
    ✅ 重掛邏輯（只在 sell_ok 時執行）
    
    規則：
    1. 重掛冷卻 5 分鐘，避免瘋狂重掛
    2. 上漲重掛：現價高於賣價 1%+，重掛到更好的價位，賺更多
    3. 下跌重掛：現價低於賣價 1%+，重掛到較低價位，但保證不虧
    4. 保底利潤：新賣價至少 = 買入價 × (1 + taker_fee + 0.02%)，絕對不虧
    """
    max_sell      = cfg["INNER_MAX_SELL_LOOP"] if layer == "inner" else cfg["OUTER_MAX_SELL_LOOP"]
    replace_count = 0

    now           = time.time()
    sell_spacing  = get_sell_spacing(inv_ratio, grid_spacing, cfg["SKEW_SELL_SPACING"])
    REPOST_COOLDOWN = 300  # 重掛冷卻 5 分鐘


    all_b    = load_batches(batches_file, log=log)
    modified = False

    for batch in all_b:
        if batch.get("layer") != layer:
            continue
        if not batch.get("sell_order_id"):
            continue

        placed_at  = batch.get("sell_placed_at", now)
        age        = now - placed_at
        sell_price = batch.get("sell_price", current_price)
        buy_price  = batch["buy_price"]

        # ── 冷卻中：跳過 ──
        if age < REPOST_COOLDOWN:
            continue

        time_out   = age > MAKER_TIMEOUT_SECS
        price_diff = (current_price - sell_price) / current_price
        price_move_up = price_diff > PRICE_MOVE_TIMEOUT
        price_move_dn = price_diff < -PRICE_MOVE_TIMEOUT

        if time_out or price_move_up or price_move_dn:
            if replace_count >= max_sell:
                continue


            state, _, _ = check_sell_filled(batch["sell_order_id"], symbol, log)
            if state in ("unknown", "filled"):
                continue

            # ── 計算新賣價 ──
            new_sell_px = get_maker_sell_price(
                buy_price, sell_spacing, best_ask, best_bid, layer, tick_size, log)

            # ── 保底利潤：至少 cover taker fee + 0.02% buffer ──
            min_profit_price = buy_price * (1 + TAKER_FEE + 0.0002)
            if new_sell_px < min_profit_price:
                # 新賣價會虧，不重掛，保留原本的賣單等價格回來
                continue

            # ── 如果新賣價跟舊賣價差不多（< 0.05%），不值得重掛 ──
            if abs(new_sell_px - sell_price) / sell_price < 0.0005:
                continue

            if price_move_up:
                reason = f"價格上漲（賣${sell_price:.2f}→新${new_sell_px:.2f}，多賺${(new_sell_px-sell_price)*batch['qty']:.4f}）"
            elif price_move_dn:
                reason = f"價格下跌（賣${sell_price:.2f}→新${new_sell_px:.2f}，仍有利潤）"
            else:
                reason = "逾時"

            log.info(f"🔄 [{layer.upper()}] 批次#{batch['id']} 重掛賣單（{reason}）")
            if free_coin < batch["qty"]:
                continue
            cancel_order(batch["sell_order_id"], symbol, log)
            batch["sell_order_id"]  = None
            batch["sell_placed_at"] = None
            modified = True

            free_coin += batch["qty"]  # 取消後釋放
            ok, oid = place_maker_sell(batch["qty"], new_sell_px, symbol, log)
            if ok:
                free_coin -= batch["qty"]  # 掛上後扣除
                batch["sell_order_id"]  = oid
                batch["sell_price"]     = new_sell_px
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
    log = logging.getLogger(f"MEXC-{coin}-BOT")
    log.propagate = False
    if not log.handlers:
        handler_file   = logging.FileHandler(f"mexc_bot_{coin.lower()}.log", encoding="utf-8")
        handler_stream = logging.StreamHandler()
        formatter      = logging.Formatter(f"%(asctime)s [MEXC-{coin}] [%(levelname)s] %(message)s")
        handler_file.setFormatter(formatter)
        handler_stream.setFormatter(formatter)
        log.addHandler(handler_file)
        log.addHandler(handler_stream)
        log.setLevel(logging.INFO)

    send_discord = make_discord_sender(symbol)

    min_p = cfg.get("TRADE_MIN_PRICE")
    max_p = cfg.get("TRADE_MAX_PRICE")
    range_str = f"${min_p:.2f}~${max_p:.2f}" if min_p and max_p else "未設定"

    log.info("=" * 66)
    log.info(f"  🚀 Jane Street v16-MEXC {symbol} Bot 啟動！")
    log.info(f"  本金：${cfg['CAPITAL']:.0f}")
    log.info(f"  外層：${cfg['OUTER_CAPITAL']:.0f} / {cfg['OUTER_MAX_BATCH']}批 / 間距{cfg['OUTER_BASE']*100}%")
    log.info(f"  費率：Taker{TAKER_FEE*100}% + Maker{MAKER_FEE*100}% = {(TAKER_FEE+MAKER_FEE)*100}%")
    log.info(f"  交易範圍：{range_str}")
    log.info("=" * 66)

    send_discord(
        f"🚀 **MEXC {symbol} Bot v16 啟動！**\n"
        f"本金：${cfg['CAPITAL']:.0f} | 內{cfg['INNER_BASE']*100}% / 外{cfg['OUTER_BASE']*100}%\n"
        f"交易範圍：{range_str}\n"
        f"Maker 0% | Taker 0.05% | Dynamic Sizing ✅"
    )

    engine      = MarketEngine(cfg)
    crash_guard = CrashGuard(cfg, send_discord)
    buy_guard   = BuyRateGuard()
    of_cache    = OrderFlowCache(symbol)

    last_price        = get_market_price(symbol, log)
    last_candle_time  = 0
    last_report_time  = 0
    usdt_balance, coin_balance, coin_free = get_balances(coin, log)
    loop_counter = 0
    inv_ratio    = 0.0

    last_buy_inner = last_price
    # 從 batches 讀取最後一筆買入價當錨點
    try:
        all_b = load_batches(batches_file, log=None)
        outer_b = [b for b in all_b if b.get("layer") == "outer"]
        if outer_b:
            last_buy_outer = float(outer_b[-1]["buy_price"])
        else:
            last_buy_outer = last_price
    except:
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
                usdt_balance, coin_balance, coin_free = get_balances(coin, log)
            of_cache.update(log)
            obi, tfi = of_cache.obi, of_cache.tfi

            trend, ema_f, ema_s = engine.get_trend()
            is_crash             = crash_guard.check(current_price, obi, tfi, log)
            regime               = engine.get_vol_regime(current_price)
            inner_spacing        = engine.get_inner_spacing(current_price, is_crash, trend)
            outer_spacing        = engine.get_outer_spacing(is_crash, trend)
            inv_ratio            = get_inv(usdt_balance, coin_balance, current_price)
            mid_price, best_bid, best_ask = get_mid_price(symbol, tick_size, log)

            in_range = is_price_in_range(cfg, current_price)

            # ✅ 動態錨點重設：接近下限快速跟價，接近上限慢慢跟
            dynamic_reset = get_dynamic_reset_mult(cfg, current_price)
            inner_reset = cfg["INNER_BASE"] * dynamic_reset
            outer_reset = cfg["OUTER_BASE"] * dynamic_reset
            if last_buy_inner > 0 and (current_price - last_buy_inner) / last_buy_inner > inner_reset:
                last_buy_inner = current_price
            if last_buy_outer > 0 and (current_price - last_buy_outer) / last_buy_outer > outer_reset:
                last_buy_outer = current_price
                log.info(f"🔄 錨點重設 → ${last_buy_outer:.4f}")

            inner_trigger, _ = get_buy_trigger(last_price, mid_price, last_buy_inner, inner_spacing)
            outer_trigger, _ = get_buy_trigger(last_price, mid_price, last_buy_outer, outer_spacing)

            all_batches   = load_batches(batches_file, log=log)
            inner_batches = [b for b in all_batches if b.get("layer") == "inner"]
            outer_batches = [b for b in all_batches if b.get("layer") == "outer"]

            inner_buy_amount = calc_buy_amount("inner", len(inner_batches), cfg, current_price, is_crash)
            outer_buy_amount = calc_buy_amount("outer", len(outer_batches), cfg, current_price, is_crash)

            zone_now       = get_price_zone(cfg, current_price)
            buy_ok  = not is_crash and inv_ratio < MAX_INVENTORY and buy_guard.can_buy() and in_range
            sell_ok = inv_ratio > MIN_INVENTORY

            # 日報
            if now - last_report_time >= 3600:
                p, t, pi, po, tp = get_today_stats(stats_file)
                fill_rate = update_metrics(t, fees_today, p, inv_ratio,
                                           maker_placed_today, maker_filled_today,
                                           metrics_file, symbol)
                target = cfg["DAILY_TARGET"]
                progress = min(p / target * 100, 100) if target > 0 else 0
                bar = "█" * int(progress / 10) + "░" * (10 - int(progress / 10))
                send_discord(
                    f"📊 **MEXC {symbol} 日報**\n"
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

                # ✅ 每小時更新 CAPITAL（用總資產計算，不只看 USDT）
                update_capital(cfg, coin, current_price, log)

            # 狀態顯示
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
                f"  🎯 區間:{zone_now.upper()}  重設:{dynamic_reset:.1f}x  {range_ok}  範圍:${cfg.get('TRADE_MIN_PRICE',0)}~${cfg.get('TRADE_MAX_PRICE',0)}\n"
                f"  🟠 外層 間距:{outer_spacing*100:.2f}% 觸發:${outer_trigger:.2f} 距離:{outer_dist:.2f}% "
                f"批次:{len(outer_batches)}/{cfg['OUTER_MAX_BATCH']} 每批:${outer_buy_amount:.2f}\n"
                f"  📖 OBI:{obi:.2f} TFI:{tfi:.2f}  "
                f"{'✅買' if buy_ok else '🚫買'}  {'✅賣' if sell_ok else '🚫賣'}\n"
                f"{'─'*66}"
            )

            # ══ 成交檢查 + 補掛（永遠執行）══
            fc_inner = check_sell_fills(
                batches_file, "inner", symbol, tick_size, best_ask, best_bid,
                inner_spacing, inv_ratio, cfg,
                stats_file, send_discord, log, free_coin=coin_free)
            fc_outer = check_sell_fills(
                batches_file, "outer", symbol, tick_size, best_ask, best_bid,
                outer_spacing, inv_ratio, cfg,
                stats_file, send_discord, log, free_coin=coin_free)
            maker_filled_today += fc_inner + fc_outer

            # ══ 賣出後重設錨點（讓下一輪從新位置觸發）══
            if fc_outer > 0:
                last_buy_outer = current_price
                log.info(f"🔄 外層賣出後錨點重設 → ${current_price:.4f}")

            # ══ 賣單管理已移除（不需要動態重掛）══

            # ══ 內層買入 ══
            if buy_ok and current_price <= inner_trigger:
                if len(inner_batches) >= get_zone_max_batch(zone_now, cfg["INNER_MAX_BATCH"]):
                    if cfg["INNER_MAX_BATCH"] > 0:
                        log.warning("⚠️  內層已達最大批次")
                else:
                    lock = acquire_usdt_lock()
                    if lock is None:
                        log.warning("⚠️  無法取得 USDT 鎖，跳過此輪買入")
                    else:
                        try:
                            usdt_balance, coin_balance, coin_free = get_balances(coin, log)
                            _, pool_remaining = calc_pool_spent(batches_file, "inner", cfg)
                            # ✅ pool 額度只作參考，真正的限制是 USDT 餘額 + MAX_INVENTORY
                            safe_amount = min(inner_buy_amount, usdt_balance)
                            if pool_remaining > 0:
                                safe_amount = min(safe_amount, pool_remaining)
                            if safe_amount < MIN_BUY_AMOUNT:
                                if usdt_balance < MIN_BUY_AMOUNT:
                                    log.warning(f"⚠️  USDT 餘額不足（${usdt_balance:.2f}）")
                                # pool 用完但 USDT 不夠，才印 warning（不是每輪都印）
                            else:
                                if cfg["INNER_MAX_BATCH"] > 0:
                                    log.info(f"📉 [內層] 買入 ${current_price:.2f} ≤ ${inner_trigger:.2f}｜${safe_amount:.2f}")
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
                                    usdt_balance, coin_balance, coin_free = get_balances(coin, log)
                                    send_discord(
                                        f"📉 **[內層] 買入！**\n"
                                        f"${fill_price:.2f} x {fill_sz:.6f} {coin}\n"
                                        f"花費：${safe_amount:.2f} | 賣單：${sell_px:.2f}"
                                    )
                        finally:
                            release_usdt_lock(lock)

            # ══ 外層買入 ══
            if buy_ok and current_price <= outer_trigger:
                if len(outer_batches) >= get_zone_max_batch(zone_now, cfg["OUTER_MAX_BATCH"]):
                    log.warning("⚠️  外層已達最大批次")
                else:
                    lock = acquire_usdt_lock()
                    if lock is None:
                        log.warning("⚠️  無法取得 USDT 鎖，跳過此輪買入")
                    else:
                        try:
                            usdt_balance, coin_balance, coin_free = get_balances(coin, log)
                            _, pool_remaining = calc_pool_spent(batches_file, "outer", cfg)
                            safe_amount = min(outer_buy_amount, usdt_balance)
                            if pool_remaining > 0:
                                safe_amount = min(safe_amount, pool_remaining)
                            if safe_amount < MIN_BUY_AMOUNT:
                                if usdt_balance < MIN_BUY_AMOUNT:
                                    log.warning(f"⚠️  USDT 餘額不足（${usdt_balance:.2f}）")
                            else:
                                log.info(f"📉 [外層] 買入 ${current_price:.2f} ≤ ${outer_trigger:.2f}｜${safe_amount:.2f}")
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
                                    usdt_balance, coin_balance, coin_free = get_balances(coin, log)
                                    send_discord(
                                        f"📉 **[外層] 買入！**\n"
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
            send_discord(f"⛔ **MEXC {symbol} Bot 已停止**｜今日+${p:.4f}({t}筆)")
            log.info(f"⛔ MEXC {symbol} Bot 停止")
            break
        except RuntimeError as e:
            log.error(f"嚴重錯誤：{e}")
            send_discord(f"🆘 **MEXC {symbol} Bot 嚴重錯誤！**\n{e}")
            time.sleep(30)
        except Exception as e:
            log.error(f"未預期錯誤：{e}")
            time.sleep(5)