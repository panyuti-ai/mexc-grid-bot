package main

import (
	"fmt"
	"os"

	"mexc-grid-bot/internal/dashboard"
	"mexc-grid-bot/internal/dotenv"
)

func main() {
	dotenv.Load()
	dir := os.Getenv("BOT_DIR")
	if dir == "" {
		home, _ := os.UserHomeDir()
		dir = home + "/AI-trading-bot"
	}
	fmt.Printf("📂 Bot 目錄：%s\n", dir)
	dashboard.Start(dir, 5566)
}
