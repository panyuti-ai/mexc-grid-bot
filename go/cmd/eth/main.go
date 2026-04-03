package main

import (
	"mexc-grid-bot/internal/config"
	"mexc-grid-bot/internal/entry"
)

func main() {
	cfg := &config.BotConfig{
		Symbol:   "ETHUSDT",
		Coin:     "ETH",
		TickSize: 0.01,

		Allocation: 0.30,

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

		VolLow:  0.002,
		VolHigh: 0.006,

		CrashDrop1M:        0.008,
		CrashRecoveryPrice: 0.004,

		DailyTarget: 5.0,

		BatchesFile: "mexc_batches_eth.json",
		StatsFile:   "mexc_stats_eth.json",
		MetricsFile: "mexc_metrics_eth.json",

		DynamicBuyMinMult: 0.5,
		DynamicBuyMaxMult: 2.5,
	}

	entry.Start(cfg)
}
