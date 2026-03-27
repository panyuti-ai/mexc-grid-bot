"""
╔══════════════════════════════════════════════════════════════════╗
║           Jane Street v16 — SOL-USDT 啟動入口                   ║
║                                                                  ║
║  交易對：SOL-USDT                                                ║
║  資金：$3300 × 55% = $1815                                       ║
║    ├─ 內層：$1270.50（70%）/ 40批 / 0.40% 間距                   ║
║    └─ 外層：$544.50（30%）/ 10批 / 1.20% 間距                    ║
║                                                                  ║
║  v16 新增：動態買入倍率（Dynamic Position Sizing）               ║
║    啟動時輸入價格上下限                                          ║
║    越接近下限 → 買入量放大（最高 2.5x）                          ║
║    越接近上限 → 買入量縮小（最低 0.5x）                          ║
║    超出範圍 → 停止買入                                            ║
║                                                                  ║
║  啟動方式：python3 bot_sol.py                                     ║
║  所有策略邏輯在 bot_base.py                                       ║
╚══════════════════════════════════════════════════════════════════╝
"""

from bot_base import run_bot, ask_price_range

SOL_CONFIG = {
    # ── 幣種識別 ──
    "SYMBOL":       "SOL-USDT",
    "COIN":         "SOL",
    "TICK_SIZE":    0.01,

    # ── 資金分配 ──
    "ALLOCATION":   0.55,
    "CAPITAL":      3300 * 0.55,           # $1815
    "INNER_CAPITAL": 3300 * 0.55 * 0.70,  # $1270.50
    "OUTER_CAPITAL": 3300 * 0.55 * 0.30,  # $544.50

    # ── 內層網格 ──
    "INNER_BASE":        40 / 10000,   # 0.40%
    "INNER_LOW_VOL":     32 / 10000,   # 0.32%
    "INNER_TREND_MULT":  1.4,
    "INNER_MAX_BATCH":   40,
    "INNER_MAX_SELL_LOOP": 3,
    "INNER_RESET_MULT":  3,

    # ── 外層網格 ──
    "OUTER_BASE":        120 / 10000,  # 1.20%
    "OUTER_TREND_MULT":  1.3,
    "OUTER_MAX_BATCH":   10,
    "OUTER_MAX_SELL_LOOP": 2,
    "OUTER_RESET_MULT":  3,

    # ── Inventory Skew ──
    "SKEW_SELL_SPACING": 30 / 10000,

    # ── Volatility Regime ──
    "VOL_LOW":  0.003,
    "VOL_HIGH": 0.008,

    # ── Crash Guard ──
    "CRASH_DROP_1M":       0.010,
    "CRASH_RECOVERY_PRICE": 0.005,

    # ── 每日收益目標 ──
    "DAILY_TARGET": 8.25,

    # ── 檔案路徑 ──
    "BATCHES_FILE": "batches_sol.json",
    "STATS_FILE":   "stats_sol.json",
    "METRICS_FILE": "metrics_sol.json",

    # ── 動態買入倍率（啟動時由使用者輸入 MIN/MAX_PRICE）──
    "DYNAMIC_BUY_MIN_MULT": 0.5,   # 接近上限時
    "DYNAMIC_BUY_MAX_MULT": 2.5,   # 接近下限時
    "TRADE_MIN_PRICE":      None,   # 啟動時輸入
    "TRADE_MAX_PRICE":      None,   # 啟動時輸入
}

if __name__ == "__main__":
    ask_price_range(SOL_CONFIG)
    run_bot(SOL_CONFIG)
