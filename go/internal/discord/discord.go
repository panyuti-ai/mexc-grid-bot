package discord

import (
	"bytes"
	"encoding/json"
	"net/http"
	"time"
)

func NewSender(webhookURL, symbol string) func(string) {
	client := &http.Client{Timeout: 5 * time.Second}
	prefix := "[MEXC-" + symbol + "] "
	return func(msg string) {
		if webhookURL == "" {
			return
		}
		body, _ := json.Marshal(map[string]string{"content": prefix + msg})
		resp, err := client.Post(webhookURL, "application/json", bytes.NewReader(body))
		if err == nil {
			resp.Body.Close()
		}
	}
}
