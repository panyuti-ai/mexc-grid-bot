package bot

import (
	"fmt"
	"io"
	"log"
	"os"
	"strconv"
	"strings"
	"time"

	"mexc-grid-bot/internal/config"
	"mexc-grid-bot/internal/mexc"
)

const (
	loopInterval    = 2 * time.Second
	balanceInterval = 10
	candleInterval  = 15 * time.Minute
	reportInterval  = time.Hour
)

// ── 帳戶餘額 ──

func getBalances(client *mexc.Client, coin string, logger *log.Logger) (usdt, coinBal, coinFree float64) {
	info, err := client.GetAccountInfo()
	if err != nil {
		logger.Printf("餘額失敗：%v", err)
		return 0, 0, 0
	}
	for _, a := range info.Balances {
		free, _ := strconv.ParseFloat(a.Free, 64)
		locked, _ := strconv.ParseFloat(a.Locked, 64)
		switch a.Asset {
		case "USDT":
			usdt = free
		case coin:
			coinFree = free
			coinBal = free + locked
		}
	}
	return
}

// getMidPrice 取 order book 的 mid, bestBid, bestAsk
func getMidPriceFromBook(client *mexc.Client, symbol string) (mid, bestBid, bestAsk float64) {
	ob, err := client.GetOrderBook(symbol, 5)
	if err != nil || len(ob.Bids) == 0 || len(ob.Asks) == 0 {
		return 0, 0, 0
	}
	bid, _ := strconv.ParseFloat(ob.Bids[0][0], 64)
	ask, _ := strconv.ParseFloat(ob.Asks[0][0], 64)
	return (bid + ask) / 2, bid, ask
}

// ── 啟動時自動偵測資本（修正版：算幣的市值）──

func AutoDetectCapital(client *mexc.Client, cfg *config.BotConfig) {
	info, err := client.GetAccountInfo()
	if err != nil {
		fmt.Printf("  ⚠️  無法偵測餘額：%v，使用預設 CAPITAL\n", err)
		return
	}
	usdt, coinBal := 0.0, 0.0
	for _, a := range info.Balances {
		free, _ := strconv.ParseFloat(a.Free, 64)
		locked, _ := strconv.ParseFloat(a.Locked, 64)
		switch a.Asset {
		case "USDT":
			usdt = free + locked
		case cfg.Coin:
			coinBal = free + locked
		}
	}

	// ✅ 修正：算總資產（USDT + 幣 × 現價），跟 update_capital 一致
	currentPrice := 0.0
	if p, err := client.GetPrice(cfg.Symbol); err == nil {
		currentPrice = p
	}
	total := usdt
	if currentPrice > 0 {
		total += coinBal * currentPrice
	}
	if total <= 0 {
		fmt.Println("  ⚠️  無法計算總資產，使用預設 CAPITAL")
		return
	}

	capital := total * cfg.Allocation
	cfg.Capital = capital
	cfg.InnerCapital = capital * 0.0
	cfg.OuterCapital = capital * 1.0

	fmt.Printf("  💰 總資產：$%.2f（USDT:$%.2f + %s:%.4f × $%.2f）\n",
		total, usdt, cfg.Coin, coinBal, currentPrice)
	fmt.Printf("  📊 %s 分配 %.0f%% = $%.2f（外層:$%.2f）\n\n",
		cfg.Coin, cfg.Allocation*100, capital, capital)
}

// ── 每小時更新 CAPITAL ──

func updateCapital(client *mexc.Client, cfg *config.BotConfig, currentPrice float64, logger *log.Logger) {
	info, err := client.GetAccountInfo()
	if err != nil {
		logger.Printf("⚠️ CAPITAL 更新失敗：%v", err)
		return
	}
	usdt, coinBal := 0.0, 0.0
	for _, a := range info.Balances {
		free, _ := strconv.ParseFloat(a.Free, 64)
		locked, _ := strconv.ParseFloat(a.Locked, 64)
		switch a.Asset {
		case "USDT":
			usdt = free + locked
		case cfg.Coin:
			coinBal = free + locked
		}
	}
	total := usdt + coinBal*currentPrice
	if total <= 0 {
		return
	}
	oldCap := cfg.Capital
	newCap := total * cfg.Allocation
	cfg.Capital = newCap
	cfg.OuterCapital = newCap

	change := 0.0
	if oldCap > 0 {
		change = (newCap - oldCap) / oldCap * 100
	}
	logger.Printf("💰 CAPITAL 更新｜總資產:$%.2f → %s 分配:$%.2f（%+.1f%%）",
		total, cfg.Coin, newCap, change)
}

// ── 主策略循環 ──

func RunBot(client *mexc.Client, cfg *config.BotConfig, send func(string)) {
	symbol := cfg.Symbol
	coin := cfg.Coin
	tickSize := cfg.TickSize

	// Logger：同時寫檔 + stdout
	logFile, _ := os.OpenFile(
		fmt.Sprintf("mexc_bot_%s.log", strings.ToLower(coin)),
		os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644,
	)
	logger := log.New(io.MultiWriter(os.Stdout, logFile),
		fmt.Sprintf("%s [MEXC-%s] ", "", coin), log.LstdFlags)

	rangeStr := fmt.Sprintf("$%.2f~$%.2f", cfg.TradeMinPrice, cfg.TradeMaxPrice)
	logger.Println(strings.Repeat("=", 66))
	logger.Printf("  🚀 Jane Street v17-Go %s Bot 啟動！", symbol)
	logger.Printf("  本金：$%.0f | 外層：$%.0f / %d批 / 間距%.2f%%",
		cfg.Capital, cfg.OuterCapital, cfg.OuterMaxBatch, cfg.OuterBase*100)
	logger.Printf("  費率：Taker %.2f%% + Maker %.2f%% | 交易範圍：%s",
		takerFee*100, makerFee*100, rangeStr)
	logger.Println(strings.Repeat("=", 66))

	send(fmt.Sprintf("🚀 **MEXC %s Bot v17-Go 啟動！**\n本金：$%.0f | 間距%.2f%%\n範圍：%s",
		symbol, cfg.Capital, cfg.OuterBase*100, rangeStr))

	engine := NewMarketEngine(cfg)
	crashGuard := NewCrashGuard(cfg, send)
	buyGuard := &BuyRateGuard{}
	ofCache := NewOrderFlowCache(symbol, client)

	// ✅ 修正：錨點用現價初始化，避免重啟立刻觸發買入
	currentPrice, _ := client.GetPrice(symbol)
	lastBuyOuter := currentPrice
	logger.Printf("  錨點初始化：$%.4f（現價）", currentPrice)

	usdt, coinBal, coinFree := getBalances(client, coin, logger)
	loopCounter := 0
	lastCandleTime := time.Time{}
	lastReportTime := time.Time{}
	lastReportDate := time.Now().Format("2006-01-02")
	makerPlacedToday := 0
	makerFilledToday := 0
	feesToday := 0.0

	for {
		iterStart := time.Now()

		// ── 取得市場資料 ──
		price, err := client.GetPrice(symbol)
		if err != nil {
			logger.Printf("價格失敗：%v", err)
			time.Sleep(loopInterval)
			continue
		}
		currentPrice = price
		crashGuard.AddPrice(currentPrice)

		// K 線（每 15 分鐘更新一次）
		if time.Since(lastCandleTime) >= candleInterval {
			klines, err := client.GetKlines(symbol, "15m", 2)
			if err == nil && len(klines) >= 2 {
				k := klines[len(klines)-2]
				toF := func(idx int) float64 {
					v, _ := strconv.ParseFloat(fmt.Sprintf("%v", k[idx]), 64)
					return v
				}
				if h := toF(2); h > 0 {
					engine.Update(h, toF(3), toF(4))
				}
			}
			lastCandleTime = time.Now()
		}

		// 餘額（每 10 輪更新）
		if loopCounter%balanceInterval == 0 {
			usdt, coinBal, coinFree = getBalances(client, coin, logger)
		}
		ofCache.Update(logger)
		obi := ofCache.OBI
		tfi := ofCache.TFI

		trend := engine.GetTrend()
		isCrash := crashGuard.Check(currentPrice, obi, tfi, logger)
		regime := engine.GetVolRegime(currentPrice)
		outerSpacing := engine.GetOuterSpacing(isCrash, trend)
		invRatio := getInv(usdt, coinBal, currentPrice)
		_, bestBid, bestAsk := getMidPriceFromBook(client, symbol)
		inRange := isPriceInRange(cfg, currentPrice)

		// 動態錨點重設
		dynamicReset := getDynamicResetMult(cfg, currentPrice)
		outerReset := cfg.OuterBase * dynamicReset
		if lastBuyOuter > 0 && (currentPrice-lastBuyOuter)/lastBuyOuter > outerReset {
			lastBuyOuter = currentPrice
			logger.Printf("🔄 錨點重設 → $%.4f", lastBuyOuter)
		}

		outerTrigger := getBuyTrigger(lastBuyOuter, 0, outerSpacing)

		allBatches := loadBatches(cfg.BatchesFile)
		outerBatches := filterLayer(allBatches, "outer")
		outerBuyAmount := calcBuyAmount("outer", len(outerBatches), cfg, currentPrice, isCrash)
		zoneNow := getPriceZone(cfg, currentPrice)

		buyOK := !isCrash && invRatio < maxInventory && buyGuard.CanBuy() && inRange
		sellOK := invRatio > minInventory

		// ── 每小時日報 + CAPITAL 更新 ──
		if time.Since(lastReportTime) >= reportInterval {
			p, t, _, _, tp := getTodayStats(cfg.StatsFile)
			fillRate := updateMetrics(t, feesToday, p, invRatio,
				makerPlacedToday, makerFilledToday, cfg.MetricsFile, symbol)
			target := cfg.DailyTarget
			progress := 0.0
			if target > 0 {
				progress = p / target * 100
				if progress > 100 {
					progress = 100
				}
			}
			send(fmt.Sprintf("📊 **MEXC %s 日報**\n今日淨利：+$%.4f USDT\n進度：[%s] %.1f%%\n交易：%d筆 | Fill:%.1f%%\n庫存：%.1f%% %s | 累計:+$%.4f",
				symbol, p, progressBar(progress), progress, t, fillRate, invRatio*100, coin, tp))
			logger.Printf("📈 日報｜+$%.4f(%d筆) Fill:%.1f%% 累計:$%.4f", p, t, fillRate, tp)
			lastReportTime = time.Now()

			today := time.Now().Format("2006-01-02")
			if today != lastReportDate {
				makerPlacedToday = 0
				makerFilledToday = 0
				feesToday = 0
				lastReportDate = today
				logger.Println("🗓️  跨日重置計數器")
			}
			updateCapital(client, cfg, currentPrice, logger)
		}

		// ── 狀態顯示 ──
		skewStr := ""
		if invRatio > inventorySkewThreshold {
			skewStr = " ⚡Skew"
		}
		crashStr := "✅"
		if isCrash {
			crashStr = fmt.Sprintf("🚨崩盤%ds", crashGuard.CooldownRemain())
		}
		trendStr := "📈正常"
		if trend == "down" {
			trendStr = "📉下跌"
		}
		regimeStr := map[string]string{"low": "🟢低波", "normal": "🟡中波", "high": "🔴高波"}[regime]
		rangeOK := "✅範圍內"
		if !inRange {
			rangeOK = "🚫超出範圍"
		}
		outerDist := (currentPrice - outerTrigger) / currentPrice * 100

		logger.Printf("\n%s\n  💰 USDT:$%.2f  %s:%.6f  庫存:%.1f%%%s\n  📊 現價:$%.2f  %s  %s  %s\n  🎯 區間:%s  重設:%.1fx  %s  範圍:$%.2f~$%.2f\n  🟠 外層 間距:%.2f%% 觸發:$%.4f 距離:%.2f%% 批次:%d/%d 每批:$%.2f\n  📖 OBI:%.2f TFI:%.2f  %s  %s\n%s",
			strings.Repeat("─", 66),
			usdt, coin, coinBal, invRatio*100, skewStr,
			currentPrice, regimeStr, trendStr, crashStr,
			strings.ToUpper(zoneNow), dynamicReset, rangeOK, cfg.TradeMinPrice, cfg.TradeMaxPrice,
			outerSpacing*100, outerTrigger, outerDist,
			len(outerBatches), cfg.OuterMaxBatch, outerBuyAmount,
			obi, tfi,
			func() string {
				if buyOK {
					return "✅買"
				}
				return "🚫買"
			}(),
			func() string {
				if sellOK {
					return "✅賣"
				}
				return "🚫賣"
			}(),
			strings.Repeat("─", 66),
		)

		// ══ 成交檢查 + 補掛（永遠執行）══
		fcOuter := checkSellFills(client,
			cfg.BatchesFile, "outer", symbol,
			tickSize, bestAsk, bestBid, outerSpacing, invRatio, cfg,
			cfg.StatsFile, send, logger, coinFree,
		)
		makerFilledToday += fcOuter

		// ✅ 賣出後錨點重設到 current_price（跟 Python 一致）
		if fcOuter > 0 {
			lastBuyOuter = currentPrice
			logger.Printf("🔄 外層賣出後錨點重設 → $%.4f", currentPrice)
		}

		// ══ 外層買入 ══
		if buyOK && currentPrice <= outerTrigger {
			zoneBatchLimit := getZoneMaxBatch(zoneNow, cfg.OuterMaxBatch)
			if len(outerBatches) >= zoneBatchLimit {
				logger.Println("⚠️  外層已達批次上限")
			} else {
				usdt, coinBal, coinFree = getBalances(client, coin, logger)
				_, poolRemaining := calcPoolSpent(cfg.BatchesFile, "outer", cfg.OuterCapital)
				safeAmount := outerBuyAmount
				if safeAmount > usdt {
					safeAmount = usdt
				}
				if poolRemaining > 0 && safeAmount > poolRemaining {
					safeAmount = poolRemaining
				}

				if safeAmount < minBuyAmount {
					if usdt < minBuyAmount {
						logger.Printf("⚠️  USDT 不足（$%.2f）", usdt)
					}
				} else {
					logger.Printf("📉 [外層] 買入 $%.4f ≤ $%.4f｜$%.2f",
						currentPrice, outerTrigger, safeAmount)
					ok, fillPrice, fillSz := executeTakerBuy(client, safeAmount, symbol, logger, send)
					if ok {
						sellSpacing := getSellSpacing(invRatio, outerSpacing, cfg.SkewSellSpacing)
						sellPx := getMakerSellPrice(fillPrice, sellSpacing, bestAsk, bestBid, "outer", tickSize)
						sOK, sID := placeMakerSell(client, fillSz, sellPx, tickSize, symbol, logger)
						sIDStr := ""
						if sOK {
							sIDStr = sID
						}
						sPxVal := 0.0
						if sOK {
							sPxVal = sellPx
						}
						addBatch(fillPrice, fillSz, safeAmount, "outer", symbol,
							cfg.BatchesFile, logger, sIDStr, sPxVal)
						lastBuyOuter = fillPrice
						buyGuard.Record()
						feesToday += fillPrice * fillSz * takerFee
						makerPlacedToday++
						usdt, coinBal, coinFree = getBalances(client, coin, logger)
						send(fmt.Sprintf("📉 **[外層] 買入！**\n$%.4f x %.6f %s\n花費：$%.2f | 賣單：$%.4f",
							fillPrice, fillSz, coin, safeAmount, sellPx))
					}
				}
			}
		}

		loopCounter++
		if elapsed := time.Since(iterStart); elapsed < loopInterval {
			time.Sleep(loopInterval - elapsed)
		}
	}
}

func progressBar(pct float64) string {
	filled := int(pct / 10)
	bar := ""
	for i := range 10 {
		if i < filled {
			bar += "█"
		} else {
			bar += "░"
		}
	}
	return bar
}
