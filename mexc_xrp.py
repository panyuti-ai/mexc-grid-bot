"""
╔══════════════════════════════════════════════════════════════════╗
║           Jane Street v16-MEXC — XRP-USDT 啟動入口              ║
║                                                                  ║
║  交易對：XRPUSDT                                                 ║
║  資金：啟動時自動偵測帳戶 USDT 餘額 × 30%                        ║
║    ├─ 內層：70% / 60批 / 0.15% 間距                              ║
║    └─ 外層：30% / 10批 / 0.45% 間距                              ║
║                                                                  ║
║  MEXC Maker 0% / Taker 0.05%                                     ║
║  淨利：0.15% - 0.05% = 0.10%/筆                                  ║
║                                                                  ║
║  XRP 特性：                                                      ║
║    - 15分鐘 K 線振幅 ~0.4%，觸發頻繁                             ║
║    - 單價 ~$1.5，精度佳                                           ║
║    - 市值 #4，$93B，機構支撐強                                    ║
║    - Goldman Sachs 持有 $1.54 億 ETF                              ║
║    - Ripple 跨境支付實際應用                                      ║
║                                                                  ║
║  啟動方式：python3 mexc_xrp.py                                    ║
╚══════════════════════════════════════════════════════════════════╝
"""

from mexc_base import run_bot, ask_price_range, auto_detect_capital

XRP_CONFIG = {
    "SYMBOL":       "XRPUSDT",
    "COIN":         "XRP",
    "TICK_SIZE":    0.0001,             # XRP 單價低，tick size 較小

    # ── 資金分配（啟動時自動偵測，這裡是 fallback）──
    "ALLOCATION":   0.30,
    "CAPITAL":      1000 * 0.30,
    "INNER_CAPITAL": 1000 * 0.30 * 0.70,
    "OUTER_CAPITAL": 1000 * 0.30 * 0.90,

    # ── 內層網格 ──
    "INNER_BASE":        15 / 10000,    # 0.15%
    "INNER_LOW_VOL":     10 / 10000,    # 0.10%
    "INNER_TREND_MULT":  1.4,
    "INNER_MAX_BATCH":   0,
    "INNER_MAX_SELL_LOOP": 5,
    "INNER_RESET_MULT":  2.5,           # 偏離 2.5×0.15% = 0.375% 重設

    # ── 外層網格 ──
    "OUTER_BASE":        20 / 10000,    # 0.20%
    "OUTER_TREND_MULT":  1.3,
    "OUTER_MAX_BATCH":   40,
    "OUTER_MAX_SELL_LOOP": 2,
    "OUTER_RESET_MULT":  2.5,           # 偏離 2.5×0.45% = 1.125% 重設

    # ── Inventory Skew ──
    "SKEW_SELL_SPACING": 10 / 10000,    # 0.10%

    # ── Volatility Regime（XRP 波動中等）──
    "VOL_LOW":  0.002,                  # ATR/price < 0.2% → 低波動
    "VOL_HIGH": 0.006,                  # ATR/price > 0.6% → 高波動

    # ── Crash Guard（XRP 波動跟 ETH 類似）──
    "CRASH_DROP_1M":       0.008,       # 60秒跌幅 > 0.8% 觸發
    "CRASH_RECOVERY_PRICE": 0.004,

    # ── 每日收益目標 ──
    "DAILY_TARGET": 3.0,

    # ── 檔案路徑（XRP 專用）──
    "BATCHES_FILE": "mexc_batches_xrp.json",
    "STATS_FILE":   "mexc_stats_xrp.json",
    "METRICS_FILE": "mexc_metrics_xrp.json",

    # ── 動態買入倍率 ──
    "DYNAMIC_BUY_MIN_MULT": 0.5,
    "DYNAMIC_BUY_MAX_MULT": 2.5,
    "TRADE_MIN_PRICE":      None,
    "TRADE_MAX_PRICE":      None,
}

if __name__ == "__main__":
    auto_detect_capital(XRP_CONFIG)
    ask_price_range(XRP_CONFIG)
    run_bot(XRP_CONFIG)
