package bot
import (
	"fmt";"io";"log";"math";"os";"strconv";"strings";"time"
	"mexc-grid-bot/internal/config"
	"mexc-grid-bot/internal/mexc"
)
const (
	loopInterval    = 200*time.Millisecond
	balanceInterval = 50
	candleInterval  = 15*time.Minute
	reportInterval  = time.Hour
)
type pendingBuy struct{orderID string;price,amount float64}
func (pb *pendingBuy) clear(){pb.orderID="";pb.price=0;pb.amount=0}
func getBalances(client *mexc.Client,coin string,logger *log.Logger)(usdt,coinBal,coinFree float64){
	info,err:=client.GetAccountInfo();if err!=nil{logger.Printf("餘額失敗:%v",err);return}
	for _,a:=range info.Balances{
		free,_:=strconv.ParseFloat(a.Free,64)
		locked,_:=strconv.ParseFloat(a.Locked,64)
		switch a.Asset{
		case "USDT":usdt=free
		case coin:coinFree=free;coinBal=free+locked}};return}
func getMidPriceFromBook(client *mexc.Client,symbol string)(mid,bid,ask float64){
	ob,err:=client.GetOrderBook(symbol,5)
	if err!=nil||len(ob.Bids)==0||len(ob.Asks)==0{return}
	b,_:=strconv.ParseFloat(ob.Bids[0][0],64)
	a,_:=strconv.ParseFloat(ob.Asks[0][0],64)
	return (b+a)/2,b,a}
func AutoDetectCapital(client *mexc.Client,cfg *config.BotConfig){
	info,err:=client.GetAccountInfo();if err!=nil{fmt.Printf("⚠️無法偵測餘額:%v\n",err);return}
	usdt,coinBal:=0.0,0.0
	for _,a:=range info.Balances{
		f,_:=strconv.ParseFloat(a.Free,64);l,_:=strconv.ParseFloat(a.Locked,64)
		switch a.Asset{case "USDT":usdt=f+l;case cfg.Coin:coinBal=f+l}}
	cur:=0.0;if p,err:=client.GetPrice(cfg.Symbol);err==nil{cur=p}
	total:=usdt+coinBal*cur;if total<=0{return}
	cap:=total*cfg.Allocation;cfg.Capital=cap;cfg.OuterCapital=cap
	fmt.Printf("  💰 總資產:$%.2f  📊 %s %.0f%%=$%.2f\n\n",total,cfg.Coin,cfg.Allocation*100,cap)}
func updateCapital(client *mexc.Client,cfg *config.BotConfig,cur float64,logger *log.Logger){
	info,err:=client.GetAccountInfo();if err!=nil{logger.Printf("CAPITAL更新失敗:%v",err);return}
	usdt,coinBal:=0.0,0.0
	for _,a:=range info.Balances{
		f,_:=strconv.ParseFloat(a.Free,64);l,_:=strconv.ParseFloat(a.Locked,64)
		switch a.Asset{case "USDT":usdt=f+l;case cfg.Coin:coinBal=f+l}}
	total:=usdt+coinBal*cur;if total<=0{return}
	old:=cfg.Capital;cfg.Capital=total*cfg.Allocation;cfg.OuterCapital=cfg.Capital
	ch:=0.0;if old>0{ch=(cfg.Capital-old)/old*100}
	logger.Printf("💰 CAPITAL 更新|總:$%.2f %s:$%.2f(%+.1f%%)",total,cfg.Coin,cfg.Capital,ch)}
func RunBot(client *mexc.Client,cfg *config.BotConfig,send func(string)){
	symbol,coin,tickSize:=cfg.Symbol,cfg.Coin,cfg.TickSize
	logFile,_:=os.OpenFile(fmt.Sprintf("mexc_bot_%s.log",strings.ToLower(coin)),os.O_APPEND|os.O_CREATE|os.O_WRONLY,0644)
	logger:=log.New(io.MultiWriter(os.Stdout,logFile),fmt.Sprintf("[MEXC-%s] ",coin),log.LstdFlags)
	store:=NewBatchStore(cfg.BatchesFile)
	feed:=mexc.NewPriceFeed(symbol,logger)
	logger.Printf("⏳ 等待 WebSocket...")
	if !feed.WaitReady(8*time.Second){logger.Printf("⚠️ WS逾時,使用REST備援")}else{logger.Printf("✅ WebSocket就緒")}
	rangeStr:=fmt.Sprintf("$%.2f~$%.2f",cfg.TradeMinPrice,cfg.TradeMaxPrice)
	logger.Println(strings.Repeat("=",60))
	logger.Printf("🚀 v18-Go %s 啟動|本金:$%.0f|間距%.2f%%|範圍:%s",symbol,cfg.Capital,cfg.OuterBase*100,rangeStr)
	logger.Println(strings.Repeat("=",60))
	send(fmt.Sprintf("🚀 **%s v18-Go**\n本金:$%.0f|間距%.2f%%\n範圍:%s",symbol,cfg.Capital,cfg.OuterBase*100,rangeStr))
	engine:=NewMarketEngine(cfg)
	crashGuard:=NewCrashGuard(cfg,send)
	ofCache:=NewOrderFlowCache(symbol,client)
	curPrice,_:=client.GetPrice(symbol)
	if t:=feed.GetTick();t.Valid(){curPrice=t.Mid()}
	lastBuyOuter:=curPrice
	logger.Printf("錨點初始化:$%.4f",curPrice)
	usdt,coinBal,coinFree:=getBalances(client,coin,logger)
	loopCnt,makerPlaced,makerFilled:=0,0,0
	feesToday:=0.0
	lastCandle:=time.Time{}
	lastReport:=time.Time{}
	lastDate:=time.Now().Format("2006-01-02")
	const numLayers=3
	var layers [numLayers]pendingBuy
	for{
		loopStart:=time.Now()
		tick:=feed.GetTick()
		if tick.Valid(){curPrice=tick.Mid()}else{
			if p,err:=client.GetPrice(symbol);err==nil{curPrice=p}else{time.Sleep(loopInterval);continue}}
		bestBid,bestAsk:=tick.BestBid,tick.BestAsk
		if !tick.Valid(){_,bestBid,bestAsk=getMidPriceFromBook(client,symbol)}
		crashGuard.AddPrice(curPrice)
		if time.Since(lastCandle)>=candleInterval{
			klines,err:=client.GetKlines(symbol,"15m",2)
			if err==nil&&len(klines)>=2{
				k:=klines[len(klines)-2]
				toF:=func(idx int)float64{v,_:=strconv.ParseFloat(fmt.Sprintf("%v",k[idx]),64);return v}
				if h:=toF(2);h>0{engine.Update(h,toF(3),toF(4))}}
			lastCandle=time.Now()}
		if loopCnt%balanceInterval==0{usdt,coinBal,coinFree=getBalances(client,coin,logger)}
		ofCache.Update(logger)
		obi,tfi:=ofCache.OBI,ofCache.TFI
		trend:=engine.GetTrend()
		isCrash:=crashGuard.Check(curPrice,obi,tfi,logger)
		regime:=engine.GetVolRegime(curPrice)
		outerSpacing:=engine.GetOuterSpacing(isCrash,trend)
		invRatio:=getInv(usdt,coinBal,curPrice)
		inRange:=isPriceInRange(cfg,curPrice)
		dynReset:=getDynamicResetMult(cfg,curPrice)
		outerReset:=cfg.OuterBase*dynReset
		if lastBuyOuter>0&&(curPrice-lastBuyOuter)/lastBuyOuter>outerReset{
			lastBuyOuter=curPrice;logger.Printf("🔄 錨點重設→$%.4f",curPrice)}
		outerTrigger:=getBuyTrigger(lastBuyOuter,0,outerSpacing)
		outerBatches:=store.Filter("outer")
		outerBuyAmt:=calcBuyAmount("outer",len(outerBatches),cfg,curPrice,isCrash)
		buyOK:=!isCrash&&invRatio<maxInventory&&inRange
		sellOK:=invRatio>minInventory
		if time.Since(lastReport)>=reportInterval{
			p,t,_,_,tp:=getTodayStats(cfg.StatsFile)
			fr:=updateMetrics(t,feesToday,p,invRatio,makerPlaced,makerFilled,cfg.MetricsFile,symbol)
			prog:=0.0;if cfg.DailyTarget>0{prog=math.Min(p/cfg.DailyTarget*100,100)}
			send(fmt.Sprintf("📊 **%s日報**\n淨利:+$%.4f [%s]%.1f%%\n交易:%d Fill:%.1f%%\n庫存:%.1f%% 累計:+$%.4f",symbol,p,progressBar(prog),prog,t,fr,invRatio*100,tp))
			lastReport=time.Now()
			if time.Now().Format("2006-01-02")!=lastDate{makerPlaced=0;makerFilled=0;feesToday=0;lastDate=time.Now().Format("2006-01-02")}
			updateCapital(client,cfg,curPrice,logger)}
		if loopCnt%20==0{
			dist:=0.0;if outerTrigger>0{dist=(curPrice-outerTrigger)/curPrice*100}
			pbStr:="無";for _,lp:=range layers{if lp.orderID!=""{pbStr=fmt.Sprintf("$%.4f #%.8s",lp.price,lp.orderID);break}}
			cStr:="✅";if isCrash{cStr=fmt.Sprintf("🚨%ds",crashGuard.CooldownRemain())}
			rMap:=map[string]string{"low":"🟢低波","normal":"🟡中波","high":"🔴高波"}
			logger.Printf("\n%s\n  💰 USDT:$%.2f %s:%.6f 庫存:%.1f%%\n  📊 $%.2f %s trend:%s %s\n  🟠 間距:%.2f%% 觸發:$%.4f 距:%.2f%% 批:%d/%d\n  📋 買單:%s OBI:%.2f TFI:%.2f %s %s\n%s",strings.Repeat("-",60),usdt,coin,coinBal,invRatio*100,curPrice,rMap[regime],trend,cStr,outerSpacing*100,outerTrigger,dist,len(outerBatches),cfg.OuterMaxBatch,pbStr,obi,tfi,map[bool]string{true:"✅買",false:"🚫買"}[buyOK],map[bool]string{true:"✅賣",false:"🚫賣"}[sellOK],strings.Repeat("-",60))}
		// sell fills
		fcO,sellPx:=checkSellFills(client,store,"outer",symbol,tickSize,bestAsk,bestBid,outerSpacing,invRatio,cfg,cfg.StatsFile,send,logger,coinFree)
		makerFilled+=fcO
		if fcO>0{if sellPx>0{lastBuyOuter=sellPx}else{lastBuyOuter=curPrice};for i:=range layers{if layers[i].orderID!=""{client.CancelOrder(symbol,layers[i].orderID);layers[i].clear()}}}
		// multi-layer pending buy
		for i:=range layers{
			lp:=&layers[i]
			lsp:=outerSpacing*float64(i+1)
			bp:=lastBuyOuter*(1-lsp)
			if lp.orderID!=""{
				st,err:=client.QueryOrder(symbol,lp.orderID)
				if err==nil{switch st.Status{
				case "FILLED":
					makerPlaced++;makerFilled++
					logger.Printf("L%d buy $%.4f",i,lp.price);lp.clear()
				case "CANCELED","EXPIRED","REJECTED":
					logger.Printf("L%d cancel:%s",i,st.Status);lp.clear()}}
				if lp.orderID!=""&&buyOK&&math.Abs(lp.price-bp)/bp>0.003{
					client.CancelOrder(symbol,lp.orderID);lp.clear()}}
		if lp.orderID==""&&buyOK&&outerBuyAmt>0&&curPrice>bp*1.0005{
				mq:=math.Floor(usdt*0.9/float64(numLayers)/bp*1e6)/1e6
				bq:=math.Min(math.Floor(outerBuyAmt/float64(numLayers)/bp*1e6)/1e6,mq)
				if bq*bp>=1.0{res,err:=client.LimitBuy(symbol,bq,bp,tickSize)
					if err==nil{lp.orderID=res.OrderID;lp.price=bp;lp.amount=bq;makerPlaced++
					logger.Printf("L%d $%.4f x%.6f",i,bp,bq)
					}else{logger.Printf("L%d err:%v",i,err)}}}}
		loopCnt++;elapsed:=time.Since(loopStart)
		if rem:=loopInterval-elapsed;rem>0{time.Sleep(rem)}
	}
}

func progressBar(pct float64) string {
	n:=int(pct/10);if n>10{n=10}
	return "["+strings.Repeat("█",n)+strings.Repeat("░",10-n)+"]"
}
