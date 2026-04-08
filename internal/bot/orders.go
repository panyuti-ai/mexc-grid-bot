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
const maxRetries = 3
type fillState int
const (
	stateLive fillState = iota
	stateFilled
	stateCanceled
	stateUnknown
)
func placeMakerSell(client *mexc.Client,qty,sellPrice,tickSize float64,symbol string,logger *log.Logger)(bool,string){
	for range maxRetries{
		result,err:=client.LimitMakerSell(symbol,qty,sellPrice,tickSize)
		if err!=nil{logger.Printf("掛賣單例外:%v",err);time.Sleep(2*time.Second);continue}
		if result.OrderID==""{logger.Printf("掛賣單失敗:%s",result.Msg);time.Sleep(2*time.Second);continue}
		logger.Printf("📌 Maker賣單 $%s x %.6f|#%s",fmtPrice(sellPrice,tickSize),qty,result.OrderID)
		return true,result.OrderID};return false,""}
func checkSellFilled(client *mexc.Client,orderID,symbol string,logger *log.Logger)(fillState,float64,float64){
	d,err:=client.QueryOrder(symbol,orderID)
	if err!=nil{logger.Printf("查詢失敗:%v",err);return stateUnknown,0,0}
	switch d.Status{
	case "FILLED":
		sz,_:=strconv.ParseFloat(d.ExecutedQty,64)
		cum,_:=strconv.ParseFloat(d.CummulativeQuoteQty,64)
		if sz>0{return stateFilled,cum/sz,sz}
	case "CANCELED","PARTIALLY_CANCELED":return stateCanceled,0,0
	case "NEW","PARTIALLY_FILLED":return stateLive,0,0
	};return stateUnknown,0,0}
var sellCheckTimes sync.Map
func checkSellFills(client *mexc.Client,store *BatchStore,layer,symbol string,tickSize,bestAsk,bestBid,gridSpacing,invRatio float64,cfg *config.BotConfig,statsFile string,send func(string),logger *log.Logger,freeCoin float64)(int,float64){
	batches:=store.Filter(layer)
	filledCount:=0;modified:=false;lastSellPx:=0.0
	nowSec:=float64(time.Now().UnixNano())/1e9
	sellSpacing:=getSellSpacing(invRatio,gridSpacing,cfg.SkewSellSpacing)
	for _,b:=range batches{
		removed:=false
		if b.SellOrderID!=""{
			key:=fmt.Sprintf("%s_%d",layer,b.ID)
			lastV,_:=sellCheckTimes.Load(key)
			last,_:=lastV.(float64)
			if nowSec-b.SellPlacedAt<10||nowSec-last<10{continue}
			sellCheckTimes.Store(key,nowSec)
			state,fillPx,_:=checkSellFilled(client,b.SellOrderID,symbol,logger)
			switch state{
			case stateFilled:
				if fillPx>0{
					profit:=(fillPx*(1-makerFee)-b.BuyPrice*(1+takerFee))*b.Qty
					recordProfit(profit,layer,statsFile)
					logTrade(symbol,layer,b.ID,b.BuyPrice,fillPx,profit)
					store.RemoveID(b.ID);removed=true;modified=true;filledCount++;if fillPx>0{lastSellPx=fillPx}
					logger.Printf("✅ [%s] 批次#%d $%.4f|+$%.4f",layer,b.ID,fillPx,profit)
					send(fmt.Sprintf("✅ **[%s] 止盈#%d**\n$%.4f→$%.4f\n+$%.4f",layer,b.ID,b.BuyPrice,fillPx,profit))}
			case stateCanceled:store.ClearSellOrder(b.ID);b.SellOrderID="";modified=true
			}}
		if !removed&&b.SellOrderID==""{
			if freeCoin<b.Qty{continue}
			sellPx:=getMakerSellPrice(b.BuyPrice,sellSpacing,bestAsk,bestBid,layer,tickSize)
			ok,oid:=placeMakerSell(client,b.Qty,sellPx,tickSize,symbol,logger)
			if ok{
				freeCoin-=b.Qty
				now:=float64(time.Now().UnixNano())/1e9
				store.UpdateSellOrder(b.ID,oid,sellPx,now)
				modified=true}}}
	if modified{store.SaveAsync()}
	return filledCount,lastSellPx}
