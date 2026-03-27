"""
╔══════════════════════════════════════════════════════════════════╗
║           Jane Street v16-MEXC — HYPE-USDT 啟動入口             ║
║                                                                  ║
║  交易對：HYPEUSDT                                                ║
║  資金：啟動時自動偵測帳戶 USDT 餘額 × 30%                        ║
║    ├─ 內層：70% / 60批 / 0.15% 間距                              ║
║    └─ 外層：30% / 10批 / 0.45% 間距                              ║
║                                                                  ║
║  MEXC Maker 0% / Taker 0.05%                                     ║
║  淨利：0.15% - 0.05% = 0.10%/筆                                  ║
║                                                                  ║
║  HYPE 特性：                                                     ║
║    - 日波動 5~8%，比 SOL 更大                                    ║
║    - 單價 ~$38，精度無問題                                        ║
║    - Hyperliquid DEX 龍頭，基本面強                               ║
║    - Crash Guard 門檻調高（正常震盪就大）                         ║
║                                                                  ║
║  啟動方式：python3 mexc_hype.py                                   ║
╚══════════════════════════════════════════════════════════════════╝
"""

from mexc_base import run_bot, ask_price_range, auto_detect_capital

HYPE_CONFIG = {
    "SYMBOL":       "HYPEUSDT",
    "COIN":         "HYPE",
    "TICK_SIZE":    0.01,

    # ── 資金分配（啟動時自動偵測，這裡是 fallback）──
    "ALLOCATION":   0.30,
    "CAPITAL":      1000 * 0.30,
    "INNER_CAPITAL": 1000 * 0.30 * 0.70,
    "OUTER_CAPITAL": 1000 * 0.30 * 0.30,

    # ── 內層網格 ──
    "INNER_BASE":        15 / 10000,    # 0.15%
    "INNER_LOW_VOL":     10 / 10000,    # 0.10%
    "INNER_TREND_MULT":  1.4,
    "INNER_MAX_BATCH":   60,
    "INNER_MAX_SELL_LOOP": 5,
    "INNER_RESET_MULT":  2.5,           # 偏離 2.5×0.15% = 0.375% 重設

    # ── 外層網格 ──
    "OUTER_BASE":        45 / 10000,    # 0.45%
    "OUTER_TREND_MULT":  1.3,
    "OUTER_MAX_BATCH":   10,
    "OUTER_MAX_SELL_LOOP": 2,
    "OUTER_RESET_MULT":  2.5,           # 偏離 2.5×0.45% = 1.125% 重設

    # ── Inventory Skew ──
    "SKEW_SELL_SPACING": 10 / 10000,    # 0.10%

    # ── Volatility Regime（HYPE 波動大，門檻調高）──
    "VOL_LOW":  0.004,                  # ATR/price < 0.4% → 低波動
    "VOL_HIGH": 0.010,                  # ATR/price > 1.0% → 高波動

    # ── Crash Guard（HYPE 正常震盪大，門檻調高避免誤觸）──
    "CRASH_DROP_1M":       0.012,       # 60秒跌幅 > 1.2% 觸發（SOL 是 1.0%）
    "CRASH_RECOVERY_PRICE": 0.006,

    # ── 每日收益目標 ──
    "DAILY_TARGET": 3.0,

    # ── 檔案路徑（HYPE 專用）──
    "BATCHES_FILE": "mexc_batches_hype.json",
    "STATS_FILE":   "mexc_stats_hype.json",
    "METRICS_FILE": "mexc_metrics_hype.json",

    # ── 動態買入倍率 ──
    "DYNAMIC_BUY_MIN_MULT": 0.5,
    "DYNAMIC_BUY_MAX_MULT": 2.5,
    "TRADE_MIN_PRICE":      None,
    "TRADE_MAX_PRICE":      None,
}

if __name__ == "__main__":
    auto_detect_capital(HYPE_CONFIG)
    ask_price_range(HYPE_CONFIG)
    run_bot(HYPE_CONFIG)
