"""
╔══════════════════════════════════════════════════════════════════╗
║           Jane Street v16-MEXC — SOL-USDT 啟動入口              ║
║                                                                  ║
║  交易對：SOLUSDT                                                  ║
║  資金：啟動時自動偵測帳戶 USDT 餘額 × 55%                        ║
║    ├─ 內層：70% / 60批 / 0.15% 間距                              ║
║    └─ 外層：30% / 10批 / 0.45% 間距                              ║
║                                                                  ║
║  MEXC Maker 0% / Taker 0.05%                                     ║
║  淨利：0.15% - 0.05% = 0.10%/筆                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

from mexc_base import run_bot, ask_price_range, auto_detect_capital

SOL_CONFIG = {
    "SYMBOL":       "SOLUSDT",
    "COIN":         "SOL",
    "TICK_SIZE":    0.01,

    # ── 資金分配（啟動時自動偵測，這裡是 fallback）──
    "ALLOCATION":   0.40,
    "CAPITAL":      1000 * 0.55,
    "INNER_CAPITAL": 1000 * 0.55 * 0.70,
    "OUTER_CAPITAL": 1000 * 0.55 * 0.90,

    "INNER_BASE":        15 / 10000,    # 0.15%
    "INNER_LOW_VOL":     10 / 10000,    # 0.10%
    "INNER_TREND_MULT":  1.4,
    "INNER_MAX_BATCH":   0,
    "INNER_MAX_SELL_LOOP": 5,
    "INNER_RESET_MULT":  2.5,

    "OUTER_BASE":        20 / 10000,    # 0.20%
    "OUTER_TREND_MULT":  1.3,
    "OUTER_MAX_BATCH":   40,
    "OUTER_MAX_SELL_LOOP": 2,
    "OUTER_RESET_MULT":  2.5,

    "SKEW_SELL_SPACING": 10 / 10000,    # 0.10%

    "VOL_LOW":  0.003,
    "VOL_HIGH": 0.008,

    "CRASH_DROP_1M":       0.010,
    "CRASH_RECOVERY_PRICE": 0.005,

    "DAILY_TARGET": 5.0,

    "BATCHES_FILE": "mexc_batches_sol.json",
    "STATS_FILE":   "mexc_stats_sol.json",
    "METRICS_FILE": "mexc_metrics_sol.json",

    "DYNAMIC_BUY_MIN_MULT": 0.5,
    "DYNAMIC_BUY_MAX_MULT": 2.5,
    "TRADE_MIN_PRICE":      None,
    "TRADE_MAX_PRICE":      None,
}

if __name__ == "__main__":
    auto_detect_capital(SOL_CONFIG)
    ask_price_range(SOL_CONFIG)
    run_bot(SOL_CONFIG)