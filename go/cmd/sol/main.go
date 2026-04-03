package main

import (
	"mexc-grid-bot/internal/config"
	"mexc-grid-bot/internal/entry"
)

func main() {
	cfg := &config.BotConfig{
		Symbol:   "SOLUSDT",
		Coin:     "SOL",
		TickSize: 0.01,

		Allocation: 0.40,

		InnerBase:        15.0 / 10000,
		InnerLowVol:      10.0 / 10000,
		InnerTrendMult:   1.4,
		InnerMaxBatch:    0,
		InnerMaxSellLoop: 5,
		InnerResetMult:   2.5,

		OuterBase:        20.0 / 10000,
		OuterTrendMult:   1.3,
		OuterMaxBatch:    40,
		OuterMaxSellLoop: 2,
		OuterResetMult:   2.5,

		SkewSellSpacing: 10.0 / 10000,

		VolLow:  0.003,
		VolHigh: 0.008,

		CrashDrop1M:        0.010,
		CrashRecoveryPrice: 0.005,

		DailyTarget: 5.0,

		BatchesFile: "mexc_batches_sol.json",
		StatsFile:   "mexc_stats_sol.json",
		MetricsFile: "mexc_metrics_sol.json",

		DynamicBuyMinMult: 0.5,
		DynamicBuyMaxMult: 2.5,
	}

	entry.Start(cfg)
}
