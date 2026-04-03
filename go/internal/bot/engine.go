package bot

import (
	"log"
	"math"
	"strconv"
	"sync"
	"time"

	"mexc-grid-bot/internal/config"
	"mexc-grid-bot/internal/mexc"
)

// ════════════════════════════════
//  MarketEngine（ATR + EMA + Vol）
// ════════════════════════════════

type MarketEngine struct {
	cfg       *config.BotConfig
	closes    []float64
	trList    []float64
	prevClose float64
	ATR       float64
}

func NewMarketEngine(cfg *config.BotConfig) *MarketEngine {
	return &MarketEngine{cfg: cfg}
}

func (e *MarketEngine) Update(high, low, close float64) {
	e.closes = append(e.closes, close)
	if len(e.closes) > 51 {
		e.closes = e.closes[len(e.closes)-51:]
	}
	if e.prevClose > 0 {
		tr := math.Max(high-low, math.Max(
			math.Abs(high-e.prevClose),
			math.Abs(low-e.prevClose),
		))
		e.trList = append(e.trList, tr)
		if len(e.trList) > 14 {
			e.trList = e.trList[len(e.trList)-14:]
		}
		if len(e.trList) >= 14 {
			sum := 0.0
			for _, v := range e.trList {
				sum += v
			}
			e.ATR = sum / float64(len(e.trList))
		}
	}
	e.prevClose = close
}

func (e *MarketEngine) ema(period int) float64 {
	if len(e.closes) < period {
		return 0
	}
	prices := e.closes[len(e.closes)-period:]
	k := 2.0 / float64(period+1)
	ema := prices[0]
	for _, p := range prices[1:] {
		ema = p*k + ema*(1-k)
	}
	return ema
}

func (e *MarketEngine) GetTrend() string {
	fast := e.ema(20)
	slow := e.ema(50)
	if fast > 0 && slow > 0 && fast < slow {
		return "down"
	}
	return "normal"
}

func (e *MarketEngine) GetVolRegime(price float64) string {
	if e.ATR == 0 || price == 0 {
		return "normal"
	}
	vol := e.ATR / price
	if vol < e.cfg.VolLow {
		return "low"
	} else if vol < e.cfg.VolHigh {
		return "normal"
	}
	return "high"
}

func (e *MarketEngine) GetInnerSpacing(price float64, isCrash bool, trend string) float64 {
	if isCrash || trend == "down" {
		return e.cfg.InnerBase * e.cfg.InnerTrendMult
	}
	switch e.GetVolRegime(price) {
	case "low":
		return e.cfg.InnerLowVol
	case "high":
		if e.ATR > 0 {
			return math.Max(e.cfg.InnerBase, e.ATR/price)
		}
	}
	return e.cfg.InnerBase
}

func (e *MarketEngine) GetOuterSpacing(isCrash bool, trend string) float64 {
	if isCrash || trend == "down" {
		return e.cfg.OuterBase * e.cfg.OuterTrendMult
	}
	return e.cfg.OuterBase
}

// ════════════════════════════════
//  CrashGuard
// ════════════════════════════════

const (
	crashOBI         = 0.15
	crashTFI         = 0.15
	crashCooldownSec = 60.0
	crashMaxCooldown = 300.0
	crashRecoveryOBI = 0.3
	crashStopDrop    = 0.003
)

type CrashGuard struct {
	cfg           *config.BotConfig
	send          func(string)
	priceHistory  []float64
	cooldownUntil float64
	crashLow      float64
	cooldownStart float64
	mu            sync.Mutex
}

func NewCrashGuard(cfg *config.BotConfig, send func(string)) *CrashGuard {
	return &CrashGuard{cfg: cfg, send: send}
}

func (g *CrashGuard) AddPrice(price float64) {
	g.mu.Lock()
	g.priceHistory = append(g.priceHistory, price)
	if len(g.priceHistory) > 30 {
		g.priceHistory = g.priceHistory[len(g.priceHistory)-30:]
	}
	g.mu.Unlock()
}

func (g *CrashGuard) drop(cur float64) float64 {
	if len(g.priceHistory) < 5 {
		return 0
	}
	mx := 0.0
	for _, p := range g.priceHistory {
		if p > mx {
			mx = p
		}
	}
	if mx == 0 {
		return 0
	}
	return (mx - cur) / mx
}

func (g *CrashGuard) Check(cur, obi, tfi float64, logger *log.Logger) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	now := float64(time.Now().UnixNano()) / 1e9
	drop := g.drop(cur)

	if obi < crashOBI && tfi < crashTFI && drop > g.cfg.CrashDrop1M {
		if now > g.cooldownUntil {
			g.crashLow = cur
			g.cooldownStart = now
			g.cooldownUntil = now + crashCooldownSec
			logger.Printf("🚨 Crash Guard！OBI:%.2f TFI:%.2f 跌幅:%.2f%%", obi, tfi, drop*100)
			g.send("🚨 **崩盤警報！**\nOBI：" + fmtF(obi) + " | TFI：" + fmtF(tfi))
		}
	}

	if now <= g.cooldownUntil {
		if now-g.cooldownStart > crashMaxCooldown {
			logger.Printf("⚠️  Crash Guard 強制解除（超過 %ds）", int(crashMaxCooldown))
			g.send("⚠️ **Crash Guard 強制解除**")
			g.cooldownUntil = 0
			g.crashLow = 0
			return false
		}
		return true
	}

	if g.cooldownUntil > 0 {
		recovery := 0.0
		if g.crashLow > 0 {
			recovery = (cur - g.crashLow) / g.crashLow
		}
		if drop < crashStopDrop && (recovery > g.cfg.CrashRecoveryPrice || obi > crashRecoveryOBI) {
			logger.Printf("✅ Crash Guard 解除｜回升:%.2f%%", recovery*100)
			g.send("✅ **崩盤警報解除**")
			g.cooldownUntil = 0
			g.crashLow = 0
		} else {
			return true
		}
	}
	return false
}

func (g *CrashGuard) CooldownRemain() int {
	remain := g.cooldownUntil - float64(time.Now().UnixNano())/1e9
	if remain < 0 {
		return 0
	}
	return int(remain)
}

func fmtF(v float64) string {
	return strconv.FormatFloat(v, 'f', 2, 64)
}

// ════════════════════════════════
//  BuyRateGuard（防插針）
// ════════════════════════════════

const maxBuy10s = 4

type BuyRateGuard struct {
	times []int64 // unix nano
	mu    sync.Mutex
}

func (g *BuyRateGuard) CanBuy() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	now := time.Now().UnixNano()
	cutoff := now - 10*int64(time.Second)
	valid := g.times[:0]
	for _, t := range g.times {
		if t > cutoff {
			valid = append(valid, t)
		}
	}
	g.times = valid
	return len(g.times) < maxBuy10s
}

func (g *BuyRateGuard) Record() {
	g.mu.Lock()
	g.times = append(g.times, time.Now().UnixNano())
	g.mu.Unlock()
}

// ════════════════════════════════
//  OrderFlowCache（OBI / TFI）
// ════════════════════════════════

const obiRefreshSec = 10

type OrderFlowCache struct {
	symbol      string
	client      *mexc.Client
	OBI         float64
	TFI         float64
	lastUpdated int64 // unix sec
	mu          sync.Mutex
}

func NewOrderFlowCache(symbol string, client *mexc.Client) *OrderFlowCache {
	return &OrderFlowCache{symbol: symbol, client: client, OBI: 0.5, TFI: 0.5}
}

func (c *OrderFlowCache) Update(logger *log.Logger) {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := time.Now().Unix()
	if now-c.lastUpdated < obiRefreshSec {
		return
	}

	ob, err := c.client.GetOrderBook(c.symbol, 20)
	if err == nil {
		bidV, askV := 0.0, 0.0
		for _, b := range ob.Bids {
			v, _ := strconv.ParseFloat(b[1], 64)
			bidV += v
		}
		for _, a := range ob.Asks {
			v, _ := strconv.ParseFloat(a[1], 64)
			askV += v
		}
		if total := bidV + askV; total > 0 {
			c.OBI = bidV / total
		}
	} else {
		logger.Printf("OBI 更新失敗：%v", err)
	}

	trades, err := c.client.GetAggTrades(c.symbol, 50)
	if err == nil {
		buyV, sellV := 0.0, 0.0
		for _, t := range trades {
			v, _ := strconv.ParseFloat(t.Qty, 64)
			if !t.IsBuyerMaker {
				buyV += v
			} else {
				sellV += v
			}
		}
		if total := buyV + sellV; total > 0 {
			c.TFI = buyV / total
		}
	} else {
		logger.Printf("TFI 更新失敗：%v", err)
	}
	c.lastUpdated = now
}
