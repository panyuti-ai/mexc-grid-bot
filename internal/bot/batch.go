package bot

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"time"
)

// Batch 對應 Python 的批次 dict
type Batch struct {
	ID           int     `json:"id"`
	Symbol       string  `json:"symbol"`
	Layer        string  `json:"layer"`
	BuyPrice     float64 `json:"buy_price"`
	Qty          float64 `json:"qty"`
	BuyUSDT      float64 `json:"buy_usdt"`
	BuyAmount    float64 `json:"buy_amount"`
	BuyTime      string  `json:"buy_time"`
	SellOrderID  string  `json:"sell_order_id"`
	SellPrice    float64 `json:"sell_price"`
	SellPlacedAt float64 `json:"sell_placed_at"`
	remove       bool    // 內部標記，不序列化
}

func loadBatches(file string) []Batch {
	for _, f := range []string{file, file + ".bak"} {
		b, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		var batches []Batch
		if err := json.Unmarshal(b, &batches); err == nil {
			return batches
		}
	}
	return []Batch{}
}

func saveBatches(batches []Batch, file string) {
	if batches == nil {
		batches = []Batch{}
	}
	// atomic write + backup
	if _, err := os.Stat(file); err == nil {
		os.Rename(file, file+".bak")
	}
	tmp := file + ".tmp"
	b, err := json.MarshalIndent(batches, "", "  ")
	if err != nil {
		return
	}
	if err := os.WriteFile(tmp, b, 0644); err != nil {
		return
	}
	os.Rename(tmp, file)
}

func filterLayer(batches []Batch, layer string) []Batch {
	out := batches[:0:0]
	for _, b := range batches {
		if b.Layer == layer {
			out = append(out, b)
		}
	}
	return out
}

func addBatch(fillPrice, fillQty, buyAmount float64,
	layer, symbol, file string, logger *log.Logger,
	sellOrderID string, sellPrice float64) Batch {

	all := loadBatches(file)
	maxID := 0
	for _, b := range all {
		if b.ID > maxID {
			maxID = b.ID
		}
	}

	placedAt := 0.0
	if sellOrderID != "" {
		placedAt = float64(time.Now().UnixNano()) / 1e9
	}

	batch := Batch{
		ID:           maxID + 1,
		Symbol:       symbol,
		Layer:        layer,
		BuyPrice:     fillPrice,
		Qty:          fillQty,
		BuyUSDT:      r6(fillPrice * fillQty),
		BuyAmount:    buyAmount,
		BuyTime:      time.Now().Format("2006-01-02 15:04:05"),
		SellOrderID:  sellOrderID,
		SellPrice:    sellPrice,
		SellPlacedAt: placedAt,
	}
	all = append(all, batch)
	saveBatches(all, file)
	logger.Printf("📝 [%s] 批次#%d｜買:$%.4f x %.6f｜掛賣:$%.4f",
		layer, batch.ID, fillPrice, fillQty, sellPrice)
	return batch
}

func calcPoolSpent(file, layer string, capital float64) (spent, remaining float64) {
	for _, b := range loadBatches(file) {
		if b.Layer == layer {
			spent += b.BuyAmount
		}
	}
	return spent, capital - spent
}

// r6 round to 6 decimal places
func r6(v float64) float64 {
	return math.Round(v*1e6) / 1e6
}

// roundToTick 四捨五入到 tickSize 精度
func roundToTick(price, tickSize float64) float64 {
	if tickSize <= 0 {
		return price
	}
	decimals := 0.0
	t := tickSize
	for t < 1 {
		t *= 10
		decimals++
	}
	factor := math.Pow(10, decimals)
	return math.Round(price*factor) / factor
}

// fmtPrice for logging
func fmtPrice(price, tickSize float64) string {
	if tickSize >= 0.01 {
		return fmt.Sprintf("%.2f", price)
	} else if tickSize >= 0.001 {
		return fmt.Sprintf("%.3f", price)
	} else if tickSize >= 0.0001 {
		return fmt.Sprintf("%.4f", price)
	}
	return fmt.Sprintf("%g", price)
}
