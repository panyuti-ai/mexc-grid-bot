"""
╔══════════════════════════════════════════════════════════════════╗
║           Jane Street v16 — ETH-USDT 啟動入口                   ║
║                                                                  ║
║  交易對：ETH-USDT                                                ║
║  資金：$3300 × 45% = $1485                                       ║
║    ├─ 內層：$1039.50（70%）/ 40批 / 0.35% 間距                   ║
║    └─ 外層：$445.50（30%）/ 10批 / 0.80% 間距                    ║
║                                                                  ║
║  v16 新增：動態買入倍率（Dynamic Position Sizing）               ║
║    啟動時輸入價格上下限                                          ║
║    越接近下限 → 買入量放大（最高 2.5x）                          ║
║    越接近上限 → 買入量縮小（最低 0.5x）                          ║
║    超出範圍 → 停止買入                                            ║
║                                                                  ║
║  啟動方式：python3 bot_eth.py                                     ║
║  所有策略邏輯在 bot_base.py                                       ║
╚══════════════════════════════════════════════════════════════════╝
"""

from bot_base import run_bot, ask_price_range

ETH_CONFIG = {
    # ── 幣種識別 ──
    "SYMBOL":       "ETH-USDT",
    "COIN":         "ETH",
    "TICK_SIZE":    0.01,

    # ── 資金分配 ──
    "ALLOCATION":   0.45,
    "CAPITAL":      3300 * 0.45,           # $1485
    "INNER_CAPITAL": 3300 * 0.45 * 0.70,  # $1039.50
    "OUTER_CAPITAL": 3300 * 0.45 * 0.30,  # $445.50

    # ── 內層網格 ──
    "INNER_BASE":        35 / 10000,   # 0.35%
    "INNER_LOW_VOL":     28 / 10000,   # 0.28%
    "INNER_TREND_MULT":  1.4,
    "INNER_MAX_BATCH":   40,
    "INNER_MAX_SELL_LOOP": 3,
    "INNER_RESET_MULT":  3,

    # ── 外層網格 ──
    "OUTER_BASE":        80 / 10000,   # 0.80%
    "OUTER_TREND_MULT":  1.3,
    "OUTER_MAX_BATCH":   10,
    "OUTER_MAX_SELL_LOOP": 2,
    "OUTER_RESET_MULT":  3,

    # ── Inventory Skew ──
    "SKEW_SELL_SPACING": 25 / 10000,

    # ── Volatility Regime ──
    "VOL_LOW":  0.002,
    "VOL_HIGH": 0.006,

    # ── Crash Guard ──
    "CRASH_DROP_1M":       0.008,
    "CRASH_RECOVERY_PRICE": 0.004,

    # ── 每日收益目標 ──
    "DAILY_TARGET": 6.75,

    # ── 檔案路徑 ──
    "BATCHES_FILE": "batches_eth.json",
    "STATS_FILE":   "stats_eth.json",
    "METRICS_FILE": "metrics_eth.json",

    # ── 動態買入倍率（啟動時由使用者輸入 MIN/MAX_PRICE）──
    "DYNAMIC_BUY_MIN_MULT": 0.5,   # 接近上限時
    "DYNAMIC_BUY_MAX_MULT": 2.5,   # 接近下限時
    "TRADE_MIN_PRICE":      None,   # 啟動時輸入
    "TRADE_MAX_PRICE":      None,   # 啟動時輸入
}

if __name__ == "__main__":
    ask_price_range(ETH_CONFIG)
    run_bot(ETH_CONFIG)
