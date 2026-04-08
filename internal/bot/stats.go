package bot

import (
	"encoding/json"
	"os"
	"time"
)

type DayStat struct {
	Trades float64 `json:"trades"`
	Profit float64 `json:"profit"`
	Inner  float64 `json:"inner"`
	Outer  float64 `json:"outer"`
	Fees   float64 `json:"fees"`
}

type Stats struct {
	TotalProfit float64            `json:"total_profit"`
	TotalTrades int                `json:"total_trades"`
	Daily       map[string]DayStat `json:"daily"`
}

func loadStats(file string) Stats {
	b, err := os.ReadFile(file)
	if err != nil {
		return Stats{Daily: map[string]DayStat{}}
	}
	var s Stats
	if err := json.Unmarshal(b, &s); err != nil {
		return Stats{Daily: map[string]DayStat{}}
	}
	if s.Daily == nil {
		s.Daily = map[string]DayStat{}
	}
	return s
}

func saveStats(s Stats, file string) {
	b, _ := json.MarshalIndent(s, "", "  ")
	tmp := file + ".tmp"
	_ = os.WriteFile(tmp, b, 0644)
	_ = os.Rename(tmp, file)
}

func recordProfit(profit float64, layer, file string) {
	s := loadStats(file)
	today := time.Now().Format("2006-01-02")
	d := s.Daily[today]
	d.Trades++
	d.Profit = r6(d.Profit + profit)
	if layer == "inner" {
		d.Inner = r6(d.Inner + profit)
	} else {
		d.Outer = r6(d.Outer + profit)
	}
	s.Daily[today] = d
	s.TotalTrades++
	s.TotalProfit = r6(s.TotalProfit + profit)
	saveStats(s, file)
}

func getTodayStats(file string) (profit float64, trades int, inner, outer, total float64) {
	s := loadStats(file)
	d := s.Daily[time.Now().Format("2006-01-02")]
	return d.Profit, int(d.Trades), d.Inner, d.Outer, s.TotalProfit
}

// ── trades_log.json（跟 Python 同路徑）──


// ── Metrics ──

func updateMetrics(trades int, fees, pnl, inv float64,
	makerPlaced, makerFilled int, file, symbol string) float64 {

	today := time.Now().Format("2006-01-02")
	fillRate := 0.0
	if makerPlaced > 0 {
		fillRate = float64(makerFilled) / float64(makerPlaced) * 100
	}

	metrics := map[string]interface{}{}
	if b, err := os.ReadFile(file); err == nil {
		_ = json.Unmarshal(b, &metrics)
	}
	metrics[today] = map[string]interface{}{
		"symbol":           symbol,
		"trades_count":     trades,
		"fees_paid":        r6(fees),
		"net_pnl":          r6(pnl),
		"inventory_ratio":  r6(inv),
		"maker_placed":     makerPlaced,
		"maker_filled":     makerFilled,
		"maker_fill_rate":  r6(fillRate),
	}
	b, _ := json.MarshalIndent(metrics, "", "  ")
	tmp := file + ".tmp"
	_ = os.WriteFile(tmp, b, 0644)
	_ = os.Rename(tmp, file)
	return fillRate
}
