package config

// BotConfig 對應 Python 的幣種 CONFIG dict
// 所有欄位名稱跟 Python key 完全對應，方便對照
type BotConfig struct {
	Symbol   string
	Coin     string
	TickSize float64

	// 資金分配
	Allocation   float64
	Capital      float64
	InnerCapital float64
	OuterCapital float64

	// 內層網格
	InnerBase        float64
	InnerLowVol      float64
	InnerTrendMult   float64
	InnerMaxBatch    int
	InnerMaxSellLoop int
	InnerResetMult   float64

	// 外層網格
	OuterBase        float64
	OuterTrendMult   float64
	OuterMaxBatch    int
	OuterMaxSellLoop int
	OuterResetMult   float64

	// Inventory Skew
	SkewSellSpacing float64

	// Volatility Regime
	VolLow  float64
	VolHigh float64

	// Crash Guard
	CrashDrop1M        float64
	CrashRecoveryPrice float64

	// 每日收益目標
	DailyTarget float64

	// 檔案路徑（格式：mexc_batches_sol.json）
	BatchesFile string
	StatsFile   string
	MetricsFile string

	// 動態買入倍率
	DynamicBuyMinMult float64
	DynamicBuyMaxMult float64
	TradeMinPrice     float64
	TradeMaxPrice     float64
}
