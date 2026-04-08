package mexc

import (
	"encoding/json"
	"log"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const wsURL = "wss://wbs.mexc.com/ws"

type Tick struct {
	BestBid float64
	BestAsk float64
}

func (t Tick) Mid() float64 {
	if t.BestBid == 0 || t.BestAsk == 0 { return 0 }
	return (t.BestBid + t.BestAsk) / 2
}

func (t Tick) Valid() bool { return t.BestBid > 0 && t.BestAsk > 0 }

type PriceFeed struct {
	symbol string
	logger *log.Logger
	mu   sync.RWMutex
	tick Tick
	ready chan struct{}
	once  sync.Once
}

func NewPriceFeed(symbol string, logger *log.Logger) *PriceFeed {
	f := &PriceFeed{symbol: symbol, logger: logger, ready: make(chan struct{})}
	go f.runLoop()
	return f
}

func (f *PriceFeed) WaitReady(d time.Duration) bool {
	select {
	case <-f.ready:
		return true
	case <-time.After(d):
		return false
	}
}

func (f *PriceFeed) GetTick() Tick {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.tick
}

func (f *PriceFeed) runLoop() {
	for {
		if err := f.connect(); err != nil {
			f.logger.Printf("[WS-%s] 斷線:%v 3s重連", f.symbol, err)
		}
		time.Sleep(3 * time.Second)
	}
}

func (f *PriceFeed) connect() error {
	conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil { return err }
	defer conn.Close()
	f.logger.Printf("[WS-%s] 已連線", f.symbol)
	sub := map[string]interface{}{
		"method": "SUBSCRIPTION",
		"params": []string{"spot@public.bookTicker.v3.api@" + f.symbol},
	}
	if err := conn.WriteJSON(sub); err != nil { return err }
	done := make(chan struct{})
	defer close(done)
	go func() {
		t := time.NewTicker(20 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-t.C:
				conn.WriteJSON(map[string]string{"method": "PING"})
			case <-done:
				return
			}
		}
	}()
	for {
		conn.SetReadDeadline(time.Now().Add(35 * time.Second))
		_, raw, err := conn.ReadMessage()
		if err != nil { return err }
		var msg struct {
			C string `json:"c"`
			D struct {
				Bid string `json:"b"`
				Ask string `json:"a"`
			} `json:"d"`
		}
		if err := json.Unmarshal(raw, &msg); err != nil { continue }
		if !strings.Contains(msg.C, "bookTicker") { continue }
		bid, _ := strconv.ParseFloat(msg.D.Bid, 64)
		ask, _ := strconv.ParseFloat(msg.D.Ask, 64)
		if bid > 0 && ask > 0 {
			f.mu.Lock()
			f.tick = Tick{BestBid: bid, BestAsk: ask}
			f.mu.Unlock()
			f.once.Do(func() { close(f.ready) })
		}
	}
}
