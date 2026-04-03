package bot

import (
	"math"

	"mexc-grid-bot/internal/config"
)

const (
	takerFee               = 0.0005 // 0.05%
	makerFee               = 0.0    // 0%
	inventorySkewThreshold = 0.50
	maxInventory           = 0.99
	minInventory           = 0.15
	minBuyAmount           = 6.0
)

// ── 三區資金配置 ──

func getPriceZone(cfg *config.BotConfig, price float64) string {
	span := cfg.TradeMaxPrice - cfg.TradeMinPrice
	if span <= 0 {
		return "mid"
	}
	if price >= cfg.TradeMaxPrice-span*0.15 {
		return "high"
	} else if price <= cfg.TradeMinPrice+span*0.25 {
		return "low"
	}
	return "mid"
}

func getZoneBatchMult(zone string, isCrash bool) float64 {
	switch zone {
	case "high":
		return 0.3
	case "low":
		if isCrash {
			return 1.2
		}
		return 1.8
	default:
		return 1.0
	}
}

func getZoneMaxBatch(zone string, baseMax int) int {
	if baseMax <= 0 {
		return 0
	}
	switch zone {
	case "high":
		return imax(int(float64(baseMax)*0.35), 5)
	case "low":
		return baseMax
	default:
		return imax(int(float64(baseMax)*0.70), 5)
	}
}

func imax(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// ── 買入金額計算 ──

func calcBuyAmount(layer string, count int, cfg *config.BotConfig, price float64, isCrash bool) float64 {
	var capital float64
	var maxBatch int
	if layer == "inner" {
		capital = cfg.InnerCapital
		maxBatch = cfg.InnerMaxBatch
	} else {
		capital = cfg.OuterCapital
		maxBatch = cfg.OuterMaxBatch
	}
	remaining := maxBatch - count
	if remaining <= 0 {
		return minBuyAmount
	}
	base := capital / float64(remaining)
	mult := getZoneBatchMult(getPriceZone(cfg, price), isCrash)
	return math.Max(base*mult, minBuyAmount)
}

// ── 動態錨點重設倍率 ──

func getDynamicResetMult(cfg *config.BotConfig, price float64) float64 {
	const resetMin = 2.0
	const resetMax = 4.0
	if cfg.TradeMaxPrice > cfg.TradeMinPrice &&
		price > cfg.TradeMinPrice && price < cfg.TradeMaxPrice {
		ratio := (price - cfg.TradeMinPrice) / (cfg.TradeMaxPrice - cfg.TradeMinPrice)
		return resetMin + ratio*(resetMax-resetMin)
	}
	return cfg.InnerResetMult
}

// ── 範圍判斷 ──

func isPriceInRange(cfg *config.BotConfig, price float64) bool {
	if cfg.TradeMinPrice > 0 && price <= cfg.TradeMinPrice {
		return false
	}
	if cfg.TradeMaxPrice > 0 && price >= cfg.TradeMaxPrice {
		return false
	}
	return true
}

// ── 報價計算 ──

func getSellSpacing(invRatio, gridSpacing, skewSpacing float64) float64 {
	if invRatio > inventorySkewThreshold {
		return skewSpacing
	}
	return gridSpacing
}

func getBuyTrigger(lastBuyPrice, midPrice, gridSpacing float64) float64 {
	if lastBuyPrice > 0 {
		return lastBuyPrice * (1 - gridSpacing)
	}
	if midPrice > 0 {
		return midPrice * (1 - gridSpacing)
	}
	return 0
}

// getMakerSellPrice 對應 Python get_maker_sell_price
// 外層：buy_price × (1+spacing)
// 內層：microspread capture，fallback 到 buy_price × (1+spacing)
func getMakerSellPrice(buyPrice, sellSpacing, bestAsk, bestBid float64,
	layer string, tickSize float64) float64 {

	minProfit := buyPrice * (1 + takerFee + 0.0002)
	minMarket := bestAsk + tickSize
	if bestAsk <= 0 {
		minMarket = minProfit
	}
	minPrice := math.Max(minProfit, minMarket)

	if layer == "outer" {
		return roundToTick(math.Max(buyPrice*(1+sellSpacing), minPrice), tickSize)
	}

	// 內層：microspread
	if bestBid > 0 && bestAsk > 0 {
		spread := bestAsk - bestBid
		if spread > 2*tickSize {
			micro := bestBid + spread*0.6
			if micro >= minPrice {
				return roundToTick(micro, tickSize)
			}
		}
		maker := bestAsk - tickSize
		if maker >= minPrice {
			return roundToTick(maker, tickSize)
		}
	}
	return roundToTick(math.Max(buyPrice*(1+sellSpacing), minPrice), tickSize)
}

func getInv(usdt, coinBal, price float64) float64 {
	total := coinBal*price + usdt
	if total <= 0 {
		return 0.5
	}
	return coinBal * price / total
}
