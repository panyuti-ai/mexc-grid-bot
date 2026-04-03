package entry

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"

	"mexc-grid-bot/internal/bot"
	"mexc-grid-bot/internal/config"
	"mexc-grid-bot/internal/discord"
	"mexc-grid-bot/internal/dotenv"
	"mexc-grid-bot/internal/mexc"
)

// Start 是所有幣種的共用啟動流程
func Start(cfg *config.BotConfig) {
	dotenv.Load()

	client := mexc.NewClient(
		os.Getenv("MEXC_API_KEY"),
		os.Getenv("MEXC_SECRET_KEY"),
	)

	// ✅ 自動偵測總資產（包含幣的市值）
	bot.AutoDetectCapital(client, cfg)

	// 輸入價格範圍
	askPriceRange(cfg, client)

	send := discord.NewSender(os.Getenv("MEXC_DISCORD_WEBHOOK_URL"), cfg.Symbol)
	bot.RunBot(client, cfg, send)
}

func askPriceRange(cfg *config.BotConfig, client *mexc.Client) {
	fmt.Printf("\n  ┌──────────────────────────────────────────┐\n")
	fmt.Printf("  │  %s 三區資金配置 (MEXC v17-Go)       │\n", cfg.Symbol)
	fmt.Printf("  │                                            │\n")
	fmt.Printf("  │  HIGH 區（上限 15%%）→ 0.3x               │\n")
	fmt.Printf("  │  MID  區（中間 60%%）→ 1.0x               │\n")
	fmt.Printf("  │  LOW  區（下限 25%%）→ 1.8x               │\n")
	fmt.Printf("  │  超出範圍 → 停止買入                       │\n")
	fmt.Printf("  └──────────────────────────────────────────┘\n\n")

	if price, err := client.GetPrice(cfg.Symbol); err == nil && price > 0 {
		fmt.Printf("  📊 %s 現價：$%.4f\n\n", cfg.Coin, price)
	}

	reader := bufio.NewReader(os.Stdin)
	for {
		fmt.Printf("  輸入 %s 價格下限（$）：", cfg.Coin)
		minStr, _ := reader.ReadString('\n')
		minPrice, err1 := strconv.ParseFloat(strings.TrimSpace(minStr), 64)

		fmt.Printf("  輸入 %s 價格上限（$）：", cfg.Coin)
		maxStr, _ := reader.ReadString('\n')
		maxPrice, err2 := strconv.ParseFloat(strings.TrimSpace(maxStr), 64)

		if err1 != nil || err2 != nil {
			fmt.Println("  ❌ 請輸入有效數字\n")
			continue
		}
		if minPrice >= maxPrice {
			fmt.Println("  ❌ 下限必須小於上限\n")
			continue
		}

		cfg.TradeMinPrice = minPrice
		cfg.TradeMaxPrice = maxPrice
		fmt.Printf("\n  ✅ 設定完成：$%.4f ~ $%.4f\n\n", minPrice, maxPrice)

		fmt.Print("  按 Enter 啟動（或輸入 q 取消）：")
		confirm, _ := reader.ReadString('\n')
		if strings.TrimSpace(confirm) == "q" {
			fmt.Println("  ⛔ 已取消")
			os.Exit(0)
		}
		break
	}
}
