package bot

import (
	"fmt"
	"log"
	"strconv"
	"sync"
	"time"

	"mexc-grid-bot/internal/config"
	"mexc-grid-bot/internal/mexc"
)

const (
	maxRetries   = 3
	fillWaitSecs = 2
)

// ── 買入（market order）──

func executeTakerBuy(client *mexc.Client, buyAmount float64,
	symbol string, logger *log.Logger, send func(string)) (ok bool, fillPrice, fillQty float64) {

	for attempt := range maxRetries {
		result, err := client.MarketBuy(symbol, buyAmount)
		if err != nil {
			logger.Printf("買入例外(%d)：%v", attempt+1, err)
			time.Sleep(2 * time.Second)
			continue
		}
		if result.OrderID == "" {
			logger.Printf("買入失敗(%d)：%s", attempt+1, result.Msg)
			time.Sleep(2 * time.Second)
			continue
		}

		time.Sleep(fillWaitSecs * time.Second)
		for range maxRetries {
			d, err := client.QueryOrder(symbol, result.OrderID)
			if err == nil && d.Status == "FILLED" {
				sz, _ := strconv.ParseFloat(d.ExecutedQty, 64)
				cum, _ := strconv.ParseFloat(d.CummulativeQuoteQty, 64)
				if sz > 0 && cum > 0 {
					avg := cum / sz
					logger.Printf("✅ 買入 $%.4f x %.6f", avg, sz)
					return true, avg, sz
				}
			}
			time.Sleep(fillWaitSecs * time.Second)
		}
		// 幽靈倉位
		msg := fmt.Sprintf("⚠️ **幽靈倉位警告！**\n訂單 #%s 已送出但查詢逾時\n請手動到 MEXC 確認", result.OrderID)
		logger.Printf("⚠️ 幽靈倉位！訂單#%s 查詢逾時", result.OrderID)
		send(msg)
		return false, 0, 0
	}
	return false, 0, 0
}

// ── 賣出（LIMIT_MAKER）──

func placeMakerSell(client *mexc.Client, qty, sellPrice float64,
	tickSize float64, symbol string, logger *log.Logger) (ok bool, orderID string) {

	for range maxRetries {
		result, err := client.LimitMakerSell(symbol, qty, sellPrice, tickSize)
		if err != nil {
			logger.Printf("掛賣單例外：%v", err)
			time.Sleep(2 * time.Second)
			continue
		}
		if result.OrderID == "" {
			logger.Printf("掛賣單失敗：%s", result.Msg)
			time.Sleep(2 * time.Second)
			continue
		}
		logger.Printf("📌 Maker賣單 $%s x %.6f｜#%s",
			fmtPrice(sellPrice, tickSize), qty, result.OrderID)
		return true, result.OrderID
	}
	return false, ""
}

// ── 查詢賣單 ──

type fillState int

const (
	stateLive fillState = iota
	stateFilled
	stateCanceled
	stateUnknown
)

func checkSellFilled(client *mexc.Client, orderID, symbol string, logger *log.Logger) (fillState, float64, float64) {
	d, err := client.QueryOrder(symbol, orderID)
	if err != nil {
		logger.Printf("查詢賣單失敗：%v", err)
		return stateUnknown, 0, 0
	}
	switch d.Status {
	case "FILLED":
		sz, _ := strconv.ParseFloat(d.ExecutedQty, 64)
		cum, _ := strconv.ParseFloat(d.CummulativeQuoteQty, 64)
		px := 0.0
		if sz > 0 {
			px = cum / sz
		}
		return stateFilled, px, sz
	case "CANCELED", "PARTIALLY_CANCELED":
		return stateCanceled, 0, 0
	case "NEW", "PARTIALLY_FILLED":
		return stateLive, 0, 0
	default:
		return stateUnknown, 0, 0
	}
}

func cancelOrder(client *mexc.Client, orderID, symbol string, logger *log.Logger) {
	if err := client.CancelOrder(symbol, orderID); err != nil {
		logger.Printf("取消失敗：%v", err)
	} else {
		logger.Printf("❌ 取消訂單 #%s", orderID)
	}
}

// ── 全域查詢冷卻（避免每輪重建）──
var sellCheckTimes sync.Map // key "layer_id" → float64 (unix sec)

// checkSellFills 對應 Python check_sell_fills
// 每輪必跑，負責：
//  1. 查詢已掛賣單是否成交
//  2. 沒有賣單的批次補掛
//
// 回傳成交筆數（錨點重設由 runner 用 current_price 處理，跟 Python 一致）
func checkSellFills(client *mexc.Client,
	batchesFile, layer, symbol string,
	tickSize, bestAsk, bestBid, gridSpacing, invRatio float64,
	cfg *config.BotConfig,
	statsFile string,
	send func(string),
	logger *log.Logger,
	freeCoin float64,
) int {

	all := loadBatches(batchesFile)
	modified := false
	filledCount := 0
	nowSec := float64(time.Now().UnixNano()) / 1e9
	sellSpacing := getSellSpacing(invRatio, gridSpacing, cfg.SkewSellSpacing)

	for i := range all {
		b := &all[i]
		if b.Layer != layer {
			continue
		}

		if b.SellOrderID != "" {
			key := fmt.Sprintf("%s_%d", layer, b.ID)
			lastCheckedV, _ := sellCheckTimes.Load(key)
			lastChecked, _ := lastCheckedV.(float64)

			// ✅ 掛單後 10 秒才查，之後每 10 秒查一次（Python 是 60s，這裡修成 10s）
			if nowSec-b.SellPlacedAt < 10 {
				continue
			}
			if nowSec-lastChecked < 10 {
				continue
			}
			sellCheckTimes.Store(key, nowSec)

			state, fillPx, _ := checkSellFilled(client, b.SellOrderID, symbol, logger)
			if state != stateLive {
				logger.Printf("🔍 [%s] 批次#%d 狀態=%d px=%.4f", layer, b.ID, state, fillPx)
			}

			switch state {
			case stateFilled:
				if fillPx > 0 {
					profit := (fillPx*(1-makerFee) - b.BuyPrice*(1+takerFee)) * b.Qty
					recordProfit(profit, layer, statsFile)
					logTrade(symbol, layer, b.ID, b.BuyPrice, fillPx, profit)
					b.remove = true
					modified = true
					filledCount++
					logger.Printf("✅ [%s] 批次#%d $%.4f｜+$%.4f", layer, b.ID, fillPx, profit)
					send(fmt.Sprintf("✅ **[%s] 止盈！批次#%d**\n$%.4f → $%.4f\n獲利：+$%.4f USDT",
						layer, b.ID, b.BuyPrice, fillPx, profit))
				}
			case stateCanceled:
				b.SellOrderID = ""
				b.SellPlacedAt = 0
				modified = true
			case stateUnknown:
				logger.Printf("⚠️ [%s] 批次#%d API unknown，跳過", layer, b.ID)
				continue
			}
		}

		// 沒有掛單 → 補掛
		if b.SellOrderID == "" && !b.remove {
			if freeCoin < b.Qty {
				continue
			}
			sellPx := getMakerSellPrice(b.BuyPrice, sellSpacing, bestAsk, bestBid, layer, tickSize)
			ok, oid := placeMakerSell(client, b.Qty, sellPx, tickSize, symbol, logger)
			if ok {
				freeCoin -= b.Qty
				b.SellOrderID = oid
				b.SellPrice = sellPx
				b.SellPlacedAt = float64(time.Now().UnixNano()) / 1e9
				modified = true
				logger.Printf("📌 [%s] 批次#%d 補掛賣單 $%.4f", layer, b.ID, sellPx)
			}
		}
	}

	if modified {
		filtered := all[:0]
		for _, b := range all {
			if !b.remove {
				filtered = append(filtered, b)
			}
		}
		saveBatches(filtered, batchesFile)
	}
	return filledCount
}
