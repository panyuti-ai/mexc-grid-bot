package bot

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"sync"
	"time"
)

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
}

type BatchStore struct {
	mu   sync.RWMutex
	data []Batch
	file string
}

func NewBatchStore(file string) *BatchStore {
	s := &BatchStore{file: file}
	s.data = s.loadFromDisk()
	return s
}
func (s *BatchStore) loadFromDisk() []Batch {
	for _,fn:=range []string{s.file,s.file+".bak"}{
		b,err:=os.ReadFile(fn);if err!=nil{continue}
		var batches []Batch
		if err:=json.Unmarshal(b,&batches);err==nil{return batches}
	};return []Batch{}}
func (s *BatchStore) Save() {
	s.mu.RLock();data:=make([]Batch,len(s.data));copy(data,s.data);s.mu.RUnlock()
	b,err:=json.MarshalIndent(data,"","  ");if err!=nil{return}
	if _,err:=os.Stat(s.file);err==nil{os.Rename(s.file,s.file+".bak")}
	tmp:=s.file+".tmp"
	if err:=os.WriteFile(tmp,b,0644);err!=nil{return}
	os.Rename(tmp,s.file)}
func (s *BatchStore) SaveAsync(){go s.Save()}
func (s *BatchStore) All() []Batch {
	s.mu.RLock();defer s.mu.RUnlock()
	cp:=make([]Batch,len(s.data));copy(cp,s.data);return cp}

func (s *BatchStore) Filter(layer string) []Batch {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var out []Batch
	for _, b := range s.data {
		if b.Layer == layer { out = append(out, b) }
	}
	return out
}

func (s *BatchStore) GetByID(id int) *Batch {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for i := range s.data {
		if s.data[i].ID == id { cp := s.data[i]; return &cp }
	}
	return nil
}

func (s *BatchStore) Add(fillPrice, fillQty, buyAmount float64, layer, symbol string, logger *log.Logger, sellOrderID string, sellPrice float64) Batch {
	s.mu.Lock()
	maxID := 0
	for _, b := range s.data {
		if b.ID > maxID { maxID = b.ID }
	}
	placedAt := 0.0
	if sellOrderID != "" { placedAt = float64(time.Now().UnixNano()) / 1e9 }
	batch := Batch{ID:maxID+1,Symbol:symbol,Layer:layer,BuyPrice:fillPrice,Qty:fillQty,BuyUSDT:r6(fillPrice*fillQty),BuyAmount:buyAmount,BuyTime:time.Now().Format("2006-01-02 15:04:05"),SellOrderID:sellOrderID,SellPrice:sellPrice,SellPlacedAt:placedAt}
	s.data = append(s.data, batch)
	s.mu.Unlock()
	logger.Printf("batch#%d buy:%.4f x %.6f sell:%.4f", batch.ID, fillPrice, fillQty, sellPrice)
	s.SaveAsync()
	return batch
}

func (s *BatchStore) RemoveID(id int) {
	s.mu.Lock()
	out := s.data[:0]
	for _, b := range s.data {
		if b.ID != id { out = append(out, b) }
	}
	s.data = out
	s.mu.Unlock()
}

func (s *BatchStore) ClearSellOrder(id int) {
	s.mu.Lock()
	for i := range s.data {
		if s.data[i].ID == id { s.data[i].SellOrderID=""; s.data[i].SellPlacedAt=0; break }
	}
	s.mu.Unlock()
}

func (s *BatchStore) UpdateSellOrder(id int, oid string, sp, pa float64) {
	s.mu.Lock()
	for i := range s.data {
		if s.data[i].ID == id {
			s.data[i].SellOrderID = oid
			s.data[i].SellPrice = sp
			s.data[i].SellPlacedAt = pa
			break
		}
	}
	s.mu.Unlock()
}

func (s *BatchStore) PoolSpent(layer string, capital float64) (float64, float64) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var spent float64
	for _, b := range s.data {
		if b.Layer == layer { spent += b.BuyAmount }
	}
	return spent, capital - spent
}

func r6(v float64) float64 { return math.Round(v*1e6)/1e6 }

func roundToTick(price, ts float64) float64 {
	if ts <= 0 { return price }
	dec := 0.0; t := ts
	for t < 1 { t *= 10; dec++ }
	factor := math.Pow(10, dec)
	return math.Round(price*factor) / factor
}

func fmtPrice(price, ts float64) string {
	if ts >= 0.01 { return fmt.Sprintf("%.2f", price) }
	if ts >= 0.001 { return fmt.Sprintf("%.3f", price) }
	if ts >= 0.0001 { return fmt.Sprintf("%.4f", price) }
	return fmt.Sprintf("%g", price)
}

const tradesLogMax = 500

type TradeLog struct {
	Time      string  `json:"time"`
	Symbol    string  `json:"symbol"`
	Layer     string  `json:"layer"`
	Batch     int     `json:"batch"`
	BuyPrice  float64 `json:"buy_price"`
	SellPrice float64 `json:"sell_price"`
	Profit    float64 `json:"profit"`
}

func logTrade(sym, layer string, id int, bp, sp, profit float64) {
	e := TradeLog{Time:time.Now().Format("2006-01-02 15:04:05"),Symbol:sym,Layer:layer,Batch:id,BuyPrice:r6(bp),SellPrice:r6(sp),Profit:r6(profit)}
	var trades []TradeLog
	if b, err := os.ReadFile("trades_log.json"); err == nil { json.Unmarshal(b, &trades) }
	trades = append(trades, e)
	if len(trades) > tradesLogMax { trades = trades[len(trades)-tradesLogMax:] }
	out, _ := json.Marshal(trades)
	os.WriteFile("trades_log.json", out, 0644)
}