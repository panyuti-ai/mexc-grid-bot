package mexc

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

const baseURL = "https://api.mexc.com"

type Client struct {
	APIKey    string
	SecretKey string
	http      *http.Client
}

func NewClient(apiKey, secretKey string) *Client {
	return &Client{
		APIKey:    apiKey,
		SecretKey: secretKey,
		http:      &http.Client{Timeout: 5 * time.Second},
	}
}

func (c *Client) sign(query string) string {
	h := hmac.New(sha256.New, []byte(c.SecretKey))
	h.Write([]byte(query))
	return fmt.Sprintf("%x", h.Sum(nil))
}

func (c *Client) ts() string {
	return strconv.FormatInt(time.Now().UnixMilli(), 10)
}

func (c *Client) get(path string, params url.Values, auth bool) ([]byte, error) {
	if auth {
		params.Set("timestamp", c.ts())
		params.Set("signature", c.sign(params.Encode()))
	}
	req, err := http.NewRequest("GET", baseURL+path+"?"+params.Encode(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-MEXC-APIKEY", c.APIKey)
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

func (c *Client) post(path string, params url.Values) ([]byte, error) {
	params.Set("timestamp", c.ts())
	params.Set("signature", c.sign(params.Encode()))
	req, err := http.NewRequest("POST", baseURL+path+"?"+params.Encode(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-MEXC-APIKEY", c.APIKey)
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

func (c *Client) del(path string, params url.Values) ([]byte, error) {
	params.Set("timestamp", c.ts())
	params.Set("signature", c.sign(params.Encode()))
	req, err := http.NewRequest("DELETE", baseURL+path+"?"+params.Encode(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-MEXC-APIKEY", c.APIKey)
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

func (c *Client) GetPrice(symbol string) (float64, error) {
	b, err := c.get("/api/v3/ticker/price", url.Values{"symbol": {symbol}}, false)
	if err != nil {
		return 0, err
	}
	var r struct {
		Price string `json:"price"`
	}
	if err := json.Unmarshal(b, &r); err != nil {
		return 0, err
	}
	return strconv.ParseFloat(r.Price, 64)
}

type OrderBook struct {
	Bids [][2]string `json:"bids"`
	Asks [][2]string `json:"asks"`
}

func (c *Client) GetOrderBook(symbol string, limit int) (*OrderBook, error) {
	b, err := c.get("/api/v3/depth", url.Values{
		"symbol": {symbol},
		"limit":  {strconv.Itoa(limit)},
	}, false)
	if err != nil {
		return nil, err
	}
	var ob OrderBook
	return &ob, json.Unmarshal(b, &ob)
}

type Kline []interface{}

func (c *Client) GetKlines(symbol, interval string, limit int) ([]Kline, error) {
	b, err := c.get("/api/v3/klines", url.Values{
		"symbol":   {symbol},
		"interval": {interval},
		"limit":    {strconv.Itoa(limit)},
	}, false)
	if err != nil {
		return nil, err
	}
	var klines []Kline
	return klines, json.Unmarshal(b, &klines)
}

type AggTrade struct {
	Qty          string `json:"q"`
	IsBuyerMaker bool   `json:"m"`
	BestMatch    bool   `json:"M"` // prevent case-insensitive collision with "m"
}

func (c *Client) GetAggTrades(symbol string, limit int) ([]AggTrade, error) {
	b, err := c.get("/api/v3/aggTrades", url.Values{
		"symbol": {symbol},
		"limit":  {strconv.Itoa(limit)},
	}, false)
	if err != nil {
		return nil, err
	}
	var trades []AggTrade
	return trades, json.Unmarshal(b, &trades)
}

type Balance struct {
	Asset  string `json:"asset"`
	Free   string `json:"free"`
	Locked string `json:"locked"`
}

type AccountInfo struct {
	Balances []Balance `json:"balances"`
}

func (c *Client) GetAccountInfo() (*AccountInfo, error) {
	b, err := c.get("/api/v3/account", url.Values{}, true)
	if err != nil {
		return nil, err
	}
	var info AccountInfo
	return &info, json.Unmarshal(b, &info)
}

type OrderResult struct {
	OrderID string `json:"orderId"`
	Msg     string `json:"msg"`
	Code    int    `json:"code"`
}

func (c *Client) MarketBuy(symbol string, quoteQty float64) (*OrderResult, error) {
	b, err := c.post("/api/v3/order", url.Values{
		"symbol":        {symbol},
		"side":          {"BUY"},
		"type":          {"MARKET"},
		"quoteOrderQty": {fmt.Sprintf("%.2f", quoteQty)},
	})
	if err != nil {
		return nil, err
	}
	var r OrderResult
	return &r, json.Unmarshal(b, &r)
}

func (c *Client) LimitMakerSell(symbol string, qty, price float64, tickSize float64) (*OrderResult, error) {
	priceStr := formatPrice(price, tickSize)
	b, err := c.post("/api/v3/order", url.Values{
		"symbol":   {symbol},
		"side":     {"SELL"},
		"type":     {"LIMIT_MAKER"},
		"quantity": {fmt.Sprintf("%.6f", qty)},
		"price":    {priceStr},
	})
	if err != nil {
		return nil, err
	}
	var r OrderResult
	return &r, json.Unmarshal(b, &r)
}

type QueryOrderResult struct {
	Status              string `json:"status"`
	ExecutedQty         string `json:"executedQty"`
	CummulativeQuoteQty string `json:"cummulativeQuoteQty"`
}

func (c *Client) QueryOrder(symbol, orderID string) (*QueryOrderResult, error) {
	b, err := c.get("/api/v3/order", url.Values{
		"symbol":  {symbol},
		"orderId": {orderID},
	}, true)
	if err != nil {
		return nil, err
	}
	var q QueryOrderResult
	return &q, json.Unmarshal(b, &q)
}

func (c *Client) CancelOrder(symbol, orderID string) error {
	_, err := c.del("/api/v3/order", url.Values{
		"symbol":  {symbol},
		"orderId": {orderID},
	})
	return err
}

func formatPrice(price, tickSize float64) string {
	if tickSize >= 1 {
		return fmt.Sprintf("%.0f", price)
	} else if tickSize >= 0.1 {
		return fmt.Sprintf("%.1f", price)
	} else if tickSize >= 0.01 {
		return fmt.Sprintf("%.2f", price)
	} else if tickSize >= 0.001 {
		return fmt.Sprintf("%.3f", price)
	} else if tickSize >= 0.0001 {
		return fmt.Sprintf("%.4f", price)
	}
	return strconv.FormatFloat(price, 'f', -1, 64)
}

func (c *Client) LimitBuy(symbol string, qty, price, tickSize float64) (*OrderResult, error) {
	priceStr := formatPrice(price, tickSize)
	b, err := c.post("/api/v3/order", url.Values{
		"symbol":      {symbol},
		"side":        {"BUY"},
		"type":        {"LIMIT_MAKER"},
		"quantity":    {fmt.Sprintf("%.6f", qty)},
		"price":       {priceStr},
		"timeInForce": {"GTC"},
	})
	if err != nil { return nil, err }
	var r OrderResult
	if err2:=json.Unmarshal(b,&r);err2!=nil{return nil,err2}
	if r.Code!=0{return nil,fmt.Errorf("MEXC %d: %s",r.Code,r.Msg)}
	return &r,nil
}
