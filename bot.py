"""
=============================================================
  AI Trading Bot — Jane Street v14 Dual-Layer Grid (3300u 專用)

  架構：雙層 Quant Grid Bot
  目標：月增 300u，睡覺期間 150~300 次交易

  雙層網格分工：
  ┌─────────────────────────────────────────────────────┐
  │  內層 Inner Grid（高頻）                             │
  │  資金池：$2310（總本金 70%）                         │
  │  最大批次：40                                        │
  │  間距：0.35%（低波 0.28%，高波自動擴張）             │
  │  每批：$2310 / 40 ≈ $57.75                          │
  │  功能：日常震盪高頻交易，貢獻大部分交易次數          │
  ├─────────────────────────────────────────────────────┤
  │  外層 Outer Grid（趨勢捕捉）                         │
  │  資金池：$990（總本金 30%）                          │
  │  最大批次：10                                        │
  │  間距：1.0%（下跌趨勢 1.3%）                        │
  │  每批：$990 / 10 ≈ $99                              │
  │  功能：捕捉大波段反彈，降低套牢風險                  │
  └─────────────────────────────────────────────────────┘

  買入：Taker（0.10%）
  賣出：Maker 限價單（0.08%）
  來回費率：0.18%

  五層保護：
  1. last_buy_price 階梯觸發（內外層各自獨立）
  2. MAX_INVENTORY 65%（內外層總和）
  3. EMA20/50 趨勢過濾（間距動態調整）
  4. Crash Guard（OBI + TFI + 60秒跌幅）
  5. MAX_BUY_10S = 4（防插針連買）

  Volatility Regime：
  低波動 → 間距縮小（提高頻率）
  高波動 → 間距擴大（避免連續接刀）
=============================================================
"""

import os
import time
import json
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

# ─────────────────────────────────────────
#  核心參數
# ─────────────────────────────────────────
SYMBOL              = "ETH-USDT"
TAKER_FEE           = 0.001
MAKER_FEE           = 0.0008
TICK_SIZE           = 0.01

TOTAL_CAPITAL       = 3300.0
INNER_CAPITAL       = TOTAL_CAPITAL * 0.70   # $2310
OUTER_CAPITAL       = TOTAL_CAPITAL * 0.30   # $990

# 內層參數
INNER_BASE          = 35 / 10000    # 0.35%
INNER_LOW_VOL       = 28 / 10000    # 0.28% 低波動時
INNER_TREND_MULT    = 1.4           # 下跌/崩盤時 0.35% × 1.4 = 0.49%
INNER_MAX_BATCH     = 40
INNER_MAX_SELL_LOOP = 3

# 外層參數
OUTER_BASE          = 100 / 10000   # 1.0%
OUTER_TREND_MULT    = 1.3           # 下跌趨勢時 1.0% × 1.3 = 1.3%
OUTER_MAX_BATCH     = 10
OUTER_MAX_SELL_LOOP = 2

# 全局庫存控制
MAX_INVENTORY       = 0.65
MIN_INVENTORY       = 0.15

# EMA 趨勢
EMA_FAST            = 20
EMA_SLOW            = 50

# ATR
ATR_PERIOD          = 14

# Volatility Regime（根據 ATR/price 判斷市場狀態）
VOL_LOW             = 0.002   # ATR/price < 0.2% → 低波動
VOL_HIGH            = 0.006   # ATR/price > 0.6% → 高波動

# Crash Guard
CRASH_OBI           = 0.15
CRASH_TFI           = 0.15
CRASH_DROP_1M       = 0.008
CRASH_COOLDOWN_SECS = 60
CRASH_RECOVERY_PRICE= 0.004
CRASH_RECOVERY_OBI  = 0.3
CRASH_STOP_DROP     = 0.003

# 防插針
MAX_BUY_10S         = 4

LOOP_INTERVAL       = 2
MAX_RETRIES         = 3
FILL_WAIT_SECS      = 2
BALANCE_INTERVAL    = 10
MIN_BUY_AMOUNT      = 10.0
OB_DEPTH            = 20
TFI_LIMIT           = 50

BATCHES_FILE        = "batches.json"
STATS_FILE          = "stats.json"

# ─────────────────────────────────────────
#  日誌
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("trading_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

marketAPI  = MarketData.MarketAPI(flag="0")
tradeAPI   = Trade.TradeAPI(API_KEY, SECRET_KEY, PASSPHRASE, flag="0")
accountAPI = Account.AccountAPI(API_KEY, SECRET_KEY, PASSPHRASE, flag="0")

API_CALL_DELAY = 0.12  # 每次 API 呼叫間隔，避免打爆 rate limit

def api_delay():
    time.sleep(API_CALL_DELAY)


# ═════════════════════════════════════════
#  Discord
# ═════════════════════════════════════════
def send_discord(msg: str):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=5)
    except Exception as e:
        log.warning(f"Discord 失敗：{e}")

def send_daily_report(profit, trades, inv, total):
    target   = 10.0
    progress = min(profit / target * 100, 100) if target > 0 else 0
    bar      = "█" * int(progress / 10) + "░" * (10 - int(progress / 10))
    send_discord(
        f"📊 **Jane Street v14 日報**\n"
        f"總資產：**${total:.2f} USDT**\n"
        f"────────────────\n"
        f"今日淨利：+${profit:.4f} USDT\n"
        f"進度：[{bar}] {progress:.1f}%\n"
        f"今日交易：{trades} 筆\n"
        f"────────────────\n"
        f"庫存：{inv*100:.1f}% ETH | 上限：65%"
    )


# ═════════════════════════════════════════
#  模組一：市場引擎（ATR + EMA + Volatility Regime）
# ═════════════════════════════════════════
class MarketEngine:
    """
    三層間距計算：
    1. Volatility Regime：低/中/高波動對應不同間距
    2. EMA 趨勢過濾：下跌趨勢間距 × TREND_MULT
    3. 崩盤保護：間距 × TREND_MULT

    用小朋友方式解釋：
    就像開車，天氣好（低波動）可以開快一點（間距小）
    天氣不好（高波動）要開慢一點（間距大）
    遇到大雨（崩盤）就停車等（間距最大）
    """
    def __init__(self):
        self.closes     = deque(maxlen=EMA_SLOW + 1)
        self.tr_list    = deque(maxlen=ATR_PERIOD)
        self.prev_close = None
        self.atr        = None

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
        k      = 2 / (period + 1)
        ema    = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema

    def get_trend(self):
        fast = self._ema(EMA_FAST)
        slow = self._ema(EMA_SLOW)
        if fast is not None and slow is not None and fast < slow:
            return "down", fast, slow
        return "normal", fast, slow

    def get_vol_regime(self, price) -> str:
        """
        判斷市場波動狀態
        low    → ATR/price < 0.2%（橫盤）
        normal → ATR/price 0.2%~0.6%（正常）
        high   → ATR/price > 0.6%（高波動）
        """
        if self.atr is None or price == 0:
            return "normal"
        vol = self.atr / price
        if vol < VOL_LOW:
            return "low"
        elif vol < VOL_HIGH:
            return "normal"
        else:
            return "high"

    def get_inner_spacing(self, price, is_crash, trend) -> float:
        """
        內層間距邏輯：
        崩盤/下跌趨勢 → INNER_BASE × 1.4
        高波動 → ATR/price（自動擴張）
        低波動 → 0.28%（提高頻率）
        正常 → 0.35%
        """
        if is_crash or trend == "down":
            return INNER_BASE * INNER_TREND_MULT

        regime = self.get_vol_regime(price)
        if regime == "low":
            return INNER_LOW_VOL
        elif regime == "high" and self.atr is not None:
            return max(INNER_BASE, self.atr / price)
        return INNER_BASE

    def get_outer_spacing(self, is_crash, trend) -> float:
        """外層間距邏輯：下跌趨勢/崩盤時擴大到 1.3%"""
        if is_crash or trend == "down":
            return OUTER_BASE * OUTER_TREND_MULT
        return OUTER_BASE


# ═════════════════════════════════════════
#  模組二：Crash Guard
# ═════════════════════════════════════════
class CrashGuard:
    def __init__(self):
        self.price_history  = deque(maxlen=30)
        self.cooldown_until = 0
        self.crash_low      = None

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

    def check(self, current_price, obi, tfi) -> bool:
        now           = time.time()
        price_drop_1m = self.get_price_drop_1m(current_price)

        if obi < CRASH_OBI and tfi < CRASH_TFI and price_drop_1m > CRASH_DROP_1M:
            if now > self.cooldown_until:
                self.crash_low      = current_price
                self.cooldown_until = now + CRASH_COOLDOWN_SECS
                log.critical(f"🚨 Crash Guard！OBI:{obi:.2f} TFI:{tfi:.2f} 跌幅:{price_drop_1m*100:.2f}%")
                send_discord(
                    f"🚨 **崩盤警報！**\n"
                    f"OBI：{obi:.2f} | TFI：{tfi:.2f}\n"
                    f"60秒跌幅：{price_drop_1m*100:.2f}%\n"
                    f"停止買入 {CRASH_COOLDOWN_SECS}秒"
                )

        if now <= self.cooldown_until:
            return True

        if self.cooldown_until > 0:
            recovery     = self.get_price_recovery(current_price)
            price_stable = price_drop_1m < CRASH_STOP_DROP
            if price_stable and (recovery > CRASH_RECOVERY_PRICE or obi > CRASH_RECOVERY_OBI):
                log.info(f"✅ Crash Guard 解除｜回升:{recovery*100:.2f}% OBI:{obi:.2f}")
                send_discord(f"✅ **崩盤警報解除**\n回升：{recovery*100:.2f}% | OBI：{obi:.2f}")
                self.cooldown_until = 0
                self.crash_low      = None
            else:
                return True

        return False


# ═════════════════════════════════════════
#  模組三：防插針連買
# ═════════════════════════════════════════
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


# ═════════════════════════════════════════
#  OBI + TFI（只用於崩盤偵測）
# ═════════════════════════════════════════
def get_obi_tfi():
    obi, tfi = 0.5, 0.5
    try:
        api_delay()
        data    = marketAPI.get_orderbook(instId=SYMBOL, sz=str(OB_DEPTH))["data"][0]
        bid_vol = sum(float(b[1]) for b in data["bids"])
        ask_vol = sum(float(a[1]) for a in data["asks"])
        total   = bid_vol + ask_vol
        if total > 0:
            obi = bid_vol / total
    except Exception as e:
        log.warning(f"OBI 失敗：{e}")

    try:
        api_delay()
        data   = marketAPI.get_trades(instId=SYMBOL, limit=str(TFI_LIMIT))["data"]
        buy_v  = sum(float(t["sz"]) for t in data if t["side"] == "buy")
        sell_v = sum(float(t["sz"]) for t in data if t["side"] == "sell")
        total  = buy_v + sell_v
        if total > 0:
            tfi = buy_v / total
    except Exception as e:
        log.warning(f"TFI 失敗：{e}")

    return obi, tfi


# ═════════════════════════════════════════
#  報價計算
# ═════════════════════════════════════════
def get_mid_price():
    try:
        api_delay()
        data     = marketAPI.get_orderbook(instId=SYMBOL, sz="1")["data"][0]
        best_bid = float(data["bids"][0][0])
        best_ask = float(data["asks"][0][0])
        return (best_bid + best_ask) / 2, best_bid, best_ask
    except:
        return 0.0, 0.0, 0.0

def get_buy_trigger(last_price, mid_price, last_buy_price, grid_spacing):
    """
    觸發點固定在錨點下方 spacing%
    錨點只在買入成交後更新，不跟著現價浮動
    """
    if last_buy_price is not None:
        return last_buy_price * (1 - grid_spacing), last_buy_price
    base = mid_price if mid_price > 0 else last_price
    return base * (1 - grid_spacing), base

def get_maker_sell_price(buy_price, grid_spacing, best_ask, layer):
    """
    ✅ Microspread Capture：
    如果 spread > 2 × tick_size，掛在 best_bid + spread×0.6（吃一半價差）
    否則掛在 best_ask - tick_size

    內層：確保有利潤後取 microspread 優化
    外層：用較大的間距，直接取 buy_price × (1 + spacing)
    """
    min_price = buy_price * (1 + TAKER_FEE + MAKER_FEE + 0.0002)

    if layer == "outer":
        # 外層直接用間距計算目標價，不做 microspread
        return round(max(buy_price * (1 + grid_spacing), min_price), 2)

    # 內層：嘗試 microspread capture
    try:
        api_delay()
        data     = marketAPI.get_orderbook(instId=SYMBOL, sz="1")["data"][0]
        bid_live = float(data["bids"][0][0])
        ask_live = float(data["asks"][0][0])
        spread   = ask_live - bid_live

        if spread > 2 * TICK_SIZE:
            micro_price = bid_live + spread * 0.6
            if micro_price >= min_price:
                return round(micro_price, 2)
    except:
        pass

    # fallback：best_ask - tick
    if best_ask > 0:
        maker_price = best_ask - TICK_SIZE
        if maker_price >= min_price:
            return round(maker_price, 2)

    return round(max(buy_price * (1 + grid_spacing), min_price), 2)


# ═════════════════════════════════════════
#  批次管理（含 layer 欄位）
# ═════════════════════════════════════════
def load_batches(layer=None):
    if os.path.exists(BATCHES_FILE):
        with open(BATCHES_FILE) as f:
            all_batches = json.load(f)
        if layer:
            return [b for b in all_batches if b.get("layer") == layer]
        return all_batches
    return []

def save_batches(batches):
    with open(BATCHES_FILE, "w") as f:
        json.dump(batches, f, indent=2)

def add_batch(fill_price, fill_qty, buy_amount, layer, sell_order_id=None, sell_price=None):
    """新增批次，layer = 'inner' 或 'outer'"""
    all_batches = load_batches()
    max_id      = max([b["id"] for b in all_batches], default=0)
    batch       = {
        "id":            max_id + 1,
        "layer":         layer,          # ✅ 區分內外層
        "buy_price":     fill_price,
        "qty":           fill_qty,
        "buy_usdt":      round(fill_price * fill_qty, 6),
        "buy_amount":    buy_amount,
        "buy_time":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sell_order_id": sell_order_id,
        "sell_price":    sell_price,
    }
    all_batches.append(batch)
    save_batches(all_batches)
    log.info(f"📝 [{layer.upper()}] 批次#{batch['id']}｜買:${fill_price:.2f} x {fill_qty:.6f}｜掛賣:${sell_price}")
    return batch

def update_batch_sell(batch_id, sell_order_id, sell_price):
    all_batches = load_batches()
    for b in all_batches:
        if b["id"] == batch_id:
            b["sell_order_id"] = sell_order_id
            b["sell_price"]    = sell_price
    save_batches(all_batches)

def remove_batch(batch_id):
    save_batches([b for b in load_batches() if b["id"] != batch_id])


# ═════════════════════════════════════════
#  統計管理
# ═════════════════════════════════════════
def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            return json.load(f)
    return {"total_profit": 0.0, "total_trades": 0, "daily": {}}

def save_stats(s):
    with open(STATS_FILE, "w") as f:
        json.dump(s, f, indent=2)

def record_profit(profit, layer):
    s     = load_stats()
    today = str(date.today())
    if today not in s["daily"]:
        s["daily"][today] = {"trades": 0, "profit": 0.0, "inner": 0.0, "outer": 0.0}
    s["daily"][today]["trades"] += 1
    s["daily"][today]["profit"]  = round(s["daily"][today]["profit"] + profit, 6)
    s["daily"][today][layer]     = round(s["daily"][today].get(layer, 0.0) + profit, 6)
    s["total_trades"] += 1
    s["total_profit"]  = round(s["total_profit"] + profit, 6)
    save_stats(s)

def get_today_stats():
    s = load_stats()
    d = s["daily"].get(str(date.today()), {"trades": 0, "profit": 0.0, "inner": 0.0, "outer": 0.0})
    return d["profit"], d["trades"], d.get("inner", 0.0), d.get("outer", 0.0), s["total_profit"]


# ═════════════════════════════════════════
#  市場 / 帳戶
# ═════════════════════════════════════════
def get_market_price():
    for i in range(MAX_RETRIES):
        try:
            api_delay()
            return float(marketAPI.get_ticker(instId=SYMBOL)["data"][0]["last"])
        except Exception as e:
            log.warning(f"價格失敗({i+1})：{e}")
            time.sleep(1)
    raise RuntimeError("無法取得市場價格")

def get_candle():
    for i in range(MAX_RETRIES):
        try:
            api_delay()
            c = marketAPI.get_candlesticks(instId=SYMBOL, bar="15m", limit="2")["data"][0]
            return float(c[2]), float(c[3]), float(c[4])
        except Exception as e:
            log.warning(f"K線失敗({i+1})：{e}")
            time.sleep(1)
    return 0.0, 0.0, 0.0

def get_balances():
    try:
        api_delay()
        details   = accountAPI.get_account_balance()["data"][0]["details"]
        usdt, eth = 0.0, 0.0
        for item in details:
            if item["ccy"] == "USDT":  usdt = float(item["availBal"])
            elif item["ccy"] == "ETH": eth  = float(item["availBal"])
        return usdt, eth
    except Exception as e:
        log.error(f"餘額失敗：{e}")
        return 0.0, 0.0

def get_inv(usdt, eth, price):
    total = eth * price + usdt
    return (eth * price / total) if total > 0 else 0.5

def calc_buy_amount(layer, batches_count):
    """
    內外層各自用自己的資金池計算每批金額
    layer = 'inner' → INNER_CAPITAL / 剩餘內層批次
    layer = 'outer' → OUTER_CAPITAL / 剩餘外層批次
    """
    if layer == "inner":
        capital   = INNER_CAPITAL
        max_batch = INNER_MAX_BATCH
    else:
        capital   = OUTER_CAPITAL
        max_batch = OUTER_MAX_BATCH
    remaining = max_batch - batches_count
    return max(capital / remaining, MIN_BUY_AMOUNT) if remaining > 0 else MIN_BUY_AMOUNT


# ═════════════════════════════════════════
#  執行訂單
# ═════════════════════════════════════════
def execute_taker_buy(buy_amount, last_price):
    for attempt in range(MAX_RETRIES):
        try:
            api_delay()
            result = tradeAPI.place_order(
                instId=SYMBOL, tdMode="cash", side="buy",
                ordType="market", sz=str(round(buy_amount, 4)), tgtCcy="quote_ccy"
            )
            if result.get("code") == "0":
                order_id = result["data"][0]["ordId"]
                time.sleep(FILL_WAIT_SECS)
                for _ in range(MAX_RETRIES):
                    api_delay()
                    d       = tradeAPI.get_order(instId=SYMBOL, ordId=order_id)["data"][0]
                    avg_px  = float(d.get("avgPx") or 0)
                    fill_sz = float(d.get("fillSz") or 0)
                    if avg_px > 0 and d.get("state") == "filled":
                        log.info(f"✅ 買入 ${avg_px:.2f} x {fill_sz:.6f}")
                        return True, avg_px, fill_sz
                    time.sleep(FILL_WAIT_SECS)
                return True, last_price, buy_amount / last_price
            else:
                log.warning(f"買入失敗({attempt+1})：{result.get('msg')}")
                time.sleep(2)
        except Exception as e:
            log.warning(f"買入例外({attempt+1})：{e}")
            time.sleep(2)
    return False, 0.0, 0.0

def place_maker_sell(qty, sell_price):
    for attempt in range(MAX_RETRIES):
        try:
            api_delay()
            result = tradeAPI.place_order(
                instId=SYMBOL, tdMode="cash", side="sell",
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

def check_sell_filled(order_id):
    try:
        api_delay()
        d = tradeAPI.get_order(instId=SYMBOL, ordId=order_id)["data"][0]
        return d.get("state", ""), float(d.get("avgPx") or 0), float(d.get("fillSz") or 0)
    except Exception as e:
        log.warning(f"查詢賣單失敗：{e}")
        return "unknown", 0.0, 0.0

def cancel_order(order_id):
    try:
        api_delay()
        tradeAPI.cancel_order(instId=SYMBOL, ordId=order_id)
        log.info(f"❌ 取消訂單 #{order_id}")
    except Exception as e:
        log.warning(f"取消失敗：{e}")


# ═════════════════════════════════════════
#  賣出邏輯（內外層各自用對應間距）
# ═════════════════════════════════════════
def process_sell_layer(batches, layer, grid_spacing, best_ask, last_price_ref):
    """
    處理指定層的賣出，回傳更新後的 last_price
    每層各自用自己的 grid_spacing 計算止盈
    """
    max_sell   = INNER_MAX_SELL_LOOP if layer == "inner" else OUTER_MAX_SELL_LOOP
    sold_count = 0
    last_price = last_price_ref

    for batch in batches[:]:
        if sold_count >= max_sell:
            break
        if batch.get("layer") != layer:
            continue

        # 有掛單 → 檢查是否成交
        if batch.get("sell_order_id"):
            state, fill_px, fill_sz = check_sell_filled(batch["sell_order_id"])
            if state == "filled" and fill_px > 0:
                profit = (fill_px * (1 - MAKER_FEE) - batch["buy_price"] * (1 + TAKER_FEE)) * batch["qty"]
                record_profit(profit, layer)
                remove_batch(batch["id"])
                last_price = fill_px
                log.info(f"✅ [{layer.upper()}] 批次#{batch['id']} ${fill_px:.2f}｜+${profit:.4f}")
                send_discord(
                    f"✅ **[{layer.upper()}] Maker止盈！批次#{batch['id']}**\n"
                    f"${batch['buy_price']:.2f} → ${fill_px:.2f}\n"
                    f"獲利：+${profit:.4f} USDT"
                )
                sold_count += 1
                continue
            elif state in ("canceled", "failed"):
                batch["sell_order_id"] = None

        # 沒有掛單 → 判斷是否掛單
        if not batch.get("sell_order_id"):
            sell_px = get_maker_sell_price(batch["buy_price"], grid_spacing, best_ask, layer)
            ok, oid = place_maker_sell(batch["qty"], sell_px)
            if ok:
                update_batch_sell(batch["id"], oid, sell_px)
                sold_count += 1

    return last_price


# ═════════════════════════════════════════
#  主策略循環
# ═════════════════════════════════════════
def run_bot():
    log.info("🚀 Jane Street Bot v14 Dual-Layer Grid 啟動！")
    log.info(f"   內層：{INNER_CAPITAL:.0f}u × {INNER_MAX_BATCH}批 間距:{INNER_BASE*100}%")
    log.info(f"   外層：{OUTER_CAPITAL:.0f}u × {OUTER_MAX_BATCH}批 間距:{OUTER_BASE*100}%")
    log.info(f"   來回費率：{(TAKER_FEE+MAKER_FEE)*100}% | 掃描：{LOOP_INTERVAL}s")

    send_discord(
        f"🚀 **Jane Street Bot v14 Dual-Layer Grid 啟動！**\n"
        f"內層：${INNER_CAPITAL:.0f}u / {INNER_MAX_BATCH}批 / 間距{INNER_BASE*100}%\n"
        f"外層：${OUTER_CAPITAL:.0f}u / {OUTER_MAX_BATCH}批 / 間距{OUTER_BASE*100}%\n"
        f"五層保護全開 ✅ 目標月增 300u 💰"
    )

    engine      = MarketEngine()
    crash_guard = CrashGuard()
    buy_guard   = BuyRateGuard()

    last_price        = get_market_price()
    last_candle_time  = 0
    last_report_time  = 0
    usdt_balance, eth_balance = get_balances()
    loop_counter = 0

    # ✅ 錨點初始化：直接用現價，不用舊批次的買入價
    # 舊批次買入價可能很低，用現價才能讓觸發點在合理位置
    last_buy_inner = last_price
    last_buy_outer = last_price
    log.info(f"   內層錨點初始化：${last_buy_inner:.2f}")

    log.info(f"   初始價格: ${last_price:.2f}")

    while True:
        try:
            current_price = get_market_price()
            now           = time.time()

            crash_guard.add_price(current_price)

            if now - last_candle_time >= 900:
                high, low, close = get_candle()
                if high > 0:
                    engine.update(high, low, close)
                    last_candle_time = now
                    log.info(f"📊 K線｜H:{high:.2f} L:{low:.2f} C:{close:.2f}")

            if loop_counter % BALANCE_INTERVAL == 0:
                usdt_balance, eth_balance = get_balances()

            # 市場狀態
            obi, tfi          = get_obi_tfi()
            trend, ema_f, ema_s = engine.get_trend()
            is_crash          = crash_guard.check(current_price, obi, tfi)
            regime            = engine.get_vol_regime(current_price)

            # 間距（內外層各自獨立）
            inner_spacing = engine.get_inner_spacing(current_price, is_crash, trend)
            outer_spacing = engine.get_outer_spacing(is_crash, trend)

            # ✅ 單向重置：只有上漲偏離超過 5% 才重置錨點
            # 下跌不重置，讓 bot 正常一路買入
            if (current_price - last_buy_inner) / last_buy_inner > 0.05:
                last_buy_inner = current_price
                log.info(f"🔄 內層錨點重設（上漲偏離）→ ${last_buy_inner:.2f}")
            if (current_price - last_buy_outer) / last_buy_outer > 0.05:
                last_buy_outer = current_price
                log.info(f"🔄 外層錨點重設（上漲偏離）→ ${last_buy_outer:.2f}")

            inv_ratio = get_inv(usdt_balance, eth_balance, current_price)
            mid_price, best_bid, best_ask = get_mid_price()

            # 買入觸發點（內外層各自獨立）
            inner_trigger, inner_base = get_buy_trigger(last_price, mid_price, last_buy_inner, inner_spacing)
            outer_trigger, _          = get_buy_trigger(last_price, mid_price, last_buy_outer, outer_spacing)

            # 批次統計
            all_batches   = load_batches()
            inner_batches = [b for b in all_batches if b.get("layer") == "inner"]
            outer_batches = [b for b in all_batches if b.get("layer") == "outer"]

            inner_buy_amount = calc_buy_amount("inner", len(inner_batches))
            outer_buy_amount = calc_buy_amount("outer", len(outer_batches))

            buy_ok   = not is_crash and inv_ratio < MAX_INVENTORY and buy_guard.can_buy()
            sell_ok  = inv_ratio > MIN_INVENTORY

            if now - last_report_time >= 3600:
                p, t, pi, po, tp = get_today_stats()
                send_daily_report(p, t, inv_ratio, usdt_balance + eth_balance * current_price)
                log.info(f"📈 日報｜今日+${p:.4f}({t}筆) inner+${pi:.4f} outer+${po:.4f}｜總+${tp:.4f}")
                last_report_time = now

            # 狀態顯示
            crash_str  = f"🚨崩盤{int(crash_guard.cooldown_until-now)}s" if is_crash else "✅"
            trend_str  = f"📉{ema_f:.0f}<{ema_s:.0f}" if trend == "down" else "📈"
            regime_str = {"low": "🟢低波", "normal": "🟡中波", "high": "🔴高波"}.get(regime, "")
            log.info(
                f"\n{'─'*70}\n"
                f"  💰 USDT:${usdt_balance:.2f}  ETH:{eth_balance:.6f}  庫存:{inv_ratio*100:.1f}%\n"
                f"  📊 現價:${current_price:.2f}  Mid:${inner_base:.2f}  {regime_str}\n"
                f"  🔵 內層 間距:{inner_spacing*100:.2f}% 觸發:${inner_trigger:.2f} 批次:{len(inner_batches)}/{INNER_MAX_BATCH} 每批:${inner_buy_amount:.2f}\n"
                f"  🟠 外層 間距:{outer_spacing*100:.2f}% 觸發:${outer_trigger:.2f} 批次:{len(outer_batches)}/{OUTER_MAX_BATCH} 每批:${outer_buy_amount:.2f}\n"
                f"  📖 OBI:{obi:.2f} TFI:{tfi:.2f} {crash_str} {trend_str}\n"
                f"  {'✅買' if buy_ok else '🚫買'}  {'✅賣' if sell_ok else '🚫賣'}\n"
                f"{'─'*70}"
            )

            # ══ 賣出（內外層各自處理）══
            if sell_ok:
                last_price = process_sell_layer(all_batches, "inner", inner_spacing, best_ask, last_price)
                last_price = process_sell_layer(all_batches, "outer", outer_spacing, best_ask, last_price)

            # ══ 內層買入 ══
            if buy_ok and current_price <= inner_trigger:
                if len(inner_batches) >= INNER_MAX_BATCH:
                    log.warning(f"⚠️  內層已達最大批次 {INNER_MAX_BATCH}")
                elif usdt_balance < inner_buy_amount:
                    log.warning(f"⚠️  USDT 不足 ${usdt_balance:.2f}")
                else:
                    log.info(f"📉 [INNER] 買入 ${current_price:.2f} ≤ ${inner_trigger:.2f}｜${inner_buy_amount:.2f}")
                    success, fill_price, fill_sz = execute_taker_buy(inner_buy_amount, last_price)
                    if success:
                        sell_px    = get_maker_sell_price(fill_price, inner_spacing, best_ask, "inner")
                        s_ok, s_id = place_maker_sell(fill_sz, sell_px)
                        add_batch(fill_price, fill_sz, inner_buy_amount, "inner",
                                  sell_order_id=s_id if s_ok else None,
                                  sell_price=sell_px if s_ok else None)
                        last_price     = fill_price
                        last_buy_inner = fill_price
                        buy_guard.record_buy()
                        usdt_balance, eth_balance = get_balances()
                        log.info(f"🔄 [INNER] 基準→${last_price:.2f}｜下次:${fill_price*(1-inner_spacing):.2f}｜賣單:${sell_px:.2f}")
                        send_discord(
                            f"📉 **[內層] 買入！批次#{len(load_batches())}**\n"
                            f"成交：${fill_price:.2f} x {fill_sz:.6f} ETH\n"
                            f"花費：${inner_buy_amount:.2f} | Maker賣單：${sell_px:.2f}\n"
                            f"下次觸發：${fill_price*(1-inner_spacing):.2f}"
                        )

            # ══ 外層買入 ══
            if buy_ok and current_price <= outer_trigger:
                if len(outer_batches) >= OUTER_MAX_BATCH:
                    log.warning(f"⚠️  外層已達最大批次 {OUTER_MAX_BATCH}")
                elif usdt_balance < outer_buy_amount:
                    log.warning(f"⚠️  USDT 不足 ${usdt_balance:.2f}")
                else:
                    log.info(f"📉 [OUTER] 買入 ${current_price:.2f} ≤ ${outer_trigger:.2f}｜${outer_buy_amount:.2f}")
                    success, fill_price, fill_sz = execute_taker_buy(outer_buy_amount, last_price)
                    if success:
                        sell_px    = get_maker_sell_price(fill_price, outer_spacing, best_ask, "outer")
                        s_ok, s_id = place_maker_sell(fill_sz, sell_px)
                        add_batch(fill_price, fill_sz, outer_buy_amount, "outer",
                                  sell_order_id=s_id if s_ok else None,
                                  sell_price=sell_px if s_ok else None)
                        last_price     = fill_price
                        last_buy_outer = fill_price
                        buy_guard.record_buy()
                        usdt_balance, eth_balance = get_balances()
                        log.info(f"🔄 [OUTER] 基準→${last_price:.2f}｜下次:${fill_price*(1-outer_spacing):.2f}｜賣單:${sell_px:.2f}")
                        send_discord(
                            f"📉 **[外層] 買入！批次#{len(load_batches())}**\n"
                            f"成交：${fill_price:.2f} x {fill_sz:.6f} ETH\n"
                            f"花費：${outer_buy_amount:.2f} | Maker賣單：${sell_px:.2f}\n"
                            f"下次觸發：${fill_price*(1-outer_spacing):.2f}"
                        )

            loop_counter += 1
            time.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            p, t, pi, po, tp = get_today_stats()
            send_daily_report(p, t, inv_ratio, usdt_balance + eth_balance * current_price)
            send_discord("⛔ **Jane Street v14 已停止**")
            log.info("⛔ Bot 停止")
            break
        except RuntimeError as e:
            log.error(f"嚴重錯誤：{e}")
            send_discord(f"🆘 **嚴重錯誤！**\n{e}")
            time.sleep(30)
        except Exception as e:
            log.error(f"未預期錯誤：{e}")
            time.sleep(5)


if __name__ == "__main__":
    run_bot()