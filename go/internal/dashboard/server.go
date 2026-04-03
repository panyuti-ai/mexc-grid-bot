package dashboard

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"
)

var botDir string
var coins = []string{"eth", "sol", "xrp"}
var symbolMap = map[string]string{
	"eth":  "ETHUSDT",
	"sol":  "SOLUSDT",
	"xrp":  "XRPUSDT",
	"hype": "HYPEUSDT",
}

func loadJSON(path string) map[string]interface{} {
	b, err := os.ReadFile(path)
	if err != nil {
		return map[string]interface{}{}
	}
	var v map[string]interface{}
	json.Unmarshal(b, &v)
	return v
}

func loadList(path string) []interface{} {
	b, err := os.ReadFile(path)
	if err != nil {
		return []interface{}{}
	}
	var v []interface{}
	json.Unmarshal(b, &v)
	return v
}

func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	json.NewEncoder(w).Encode(v)
}

func statsHandler(w http.ResponseWriter, r *http.Request) {
	result := map[string]interface{}{}
	totalProfit := 0.0
	totalTrades := 0.0

	for _, coin := range coins {
		s := loadJSON(filepath.Join(botDir, fmt.Sprintf("mexc_stats_%s.json", coin)))
		daily := map[string]interface{}{}
		if d, ok := s["daily"].(map[string]interface{}); ok {
			for date, v := range d {
				daily[date] = v
			}
		}
		result[coin] = map[string]interface{}{
			"total_profit": s["total_profit"],
			"total_trades": s["total_trades"],
			"daily":        daily,
		}
		if p, ok := s["total_profit"].(float64); ok {
			totalProfit += p
		}
		if t, ok := s["total_trades"].(float64); ok {
			totalTrades += t
		}
	}
	result["summary"] = map[string]interface{}{
		"total_profit": totalProfit,
		"total_trades": totalTrades,
		"updated_at":   time.Now().Format("2006-01-02 15:04:05"),
	}
	writeJSON(w, result)
}

func tradesHandler(w http.ResponseWriter, r *http.Request) {
	b, err := os.ReadFile(filepath.Join(botDir, "trades_log.json"))
	if err != nil {
		writeJSON(w, []interface{}{})
		return
	}
	var trades []interface{}
	json.Unmarshal(b, &trades)

	limit := 200
	if l := r.URL.Query().Get("limit"); l != "" {
		if n, err := strconv.Atoi(l); err == nil {
			limit = n
		}
	}
	// reverse
	for i, j := 0, len(trades)-1; i < j; i, j = i+1, j-1 {
		trades[i], trades[j] = trades[j], trades[i]
	}
	if limit < len(trades) {
		trades = trades[:limit]
	}
	writeJSON(w, trades)
}

func batchesHandler(w http.ResponseWriter, r *http.Request) {
	result := map[string]interface{}{}
	for _, coin := range coins {
		result[coin] = loadList(filepath.Join(botDir, fmt.Sprintf("mexc_batches_%s.json", coin)))
	}
	writeJSON(w, result)
}

func allHandler(w http.ResponseWriter, r *http.Request) {
	coin := r.PathValue("coin")
	symbol, ok := symbolMap[coin]
	if !ok {
		http.Error(w, "unknown coin", 400)
		return
	}

	// 取現價
	priceVal := 0.0
	resp, err := http.Get(fmt.Sprintf("https://api.mexc.com/api/v3/ticker/price?symbol=%s", symbol))
	if err == nil {
		defer resp.Body.Close()
		var pr struct {
			Price string `json:"price"`
		}
		if b, err := io.ReadAll(resp.Body); err == nil {
			json.Unmarshal(b, &pr)
			priceVal, _ = strconv.ParseFloat(pr.Price, 64)
		}
	}

	// K 線
	interval := r.URL.Query().Get("interval")
	if interval == "" {
		interval = "15m"
	}
	candles := []interface{}{}
	resp2, err := http.Get(fmt.Sprintf(
		"https://api.mexc.com/api/v3/klines?symbol=%s&interval=%s&limit=150",
		symbol, interval))
	if err == nil {
		defer resp2.Body.Close()
		var raw [][]interface{}
		if b, err := io.ReadAll(resp2.Body); err == nil {
			json.Unmarshal(b, &raw)
			for _, c := range raw {
				if len(c) < 6 {
					continue
				}
				t, _ := c[0].(float64)
				open, _ := strconv.ParseFloat(fmt.Sprintf("%v", c[1]), 64)
				high, _ := strconv.ParseFloat(fmt.Sprintf("%v", c[2]), 64)
				low, _ := strconv.ParseFloat(fmt.Sprintf("%v", c[3]), 64)
				close, _ := strconv.ParseFloat(fmt.Sprintf("%v", c[4]), 64)
				vol, _ := strconv.ParseFloat(fmt.Sprintf("%v", c[5]), 64)
				candles = append(candles, map[string]interface{}{
					"time":   int64(t) / 1000,
					"open":   open,
					"high":   high,
					"low":    low,
					"close":  close,
					"volume": vol,
				})
			}
		}
	}

	writeJSON(w, map[string]interface{}{
		"coin":    coin,
		"symbol":  symbol,
		"price":   priceVal,
		"candles": candles,
		"batches": loadList(filepath.Join(botDir, fmt.Sprintf("mexc_batches_%s.json", coin))),
		"stats":   loadJSON(filepath.Join(botDir, fmt.Sprintf("mexc_stats_%s.json", coin))),
		"metrics": loadJSON(filepath.Join(botDir, fmt.Sprintf("mexc_metrics_%s.json", coin))),
	})
}

func Start(dir string, port int) {
	botDir = dir

	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/stats", statsHandler)
	mux.HandleFunc("GET /api/trades", tradesHandler)
	mux.HandleFunc("GET /api/batches", batchesHandler)
	mux.HandleFunc("GET /api/all/{coin}", allHandler)
	mux.HandleFunc("GET /lw-charts.js", func(w http.ResponseWriter, r *http.Request) {
		http.ServeFile(w, r, filepath.Join(dir, "lw-charts.js"))
	})
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		http.ServeFile(w, r, filepath.Join(dir, "dashboard.html"))
	})

	addr := fmt.Sprintf("0.0.0.0:%d", port)
	fmt.Printf("🚀 Dashboard 啟動於 http://%s\n", addr)
	http.ListenAndServe(addr, mux)
}
