# MEXC Grid Trading Bot - v18-Go

Automated crypto grid trading bot written in **Go**, running on MEXC exchange.
Trading ETH/USDT and SOL/USDT with real capital on a VPS.

---

## Why Go?

Originally Python (v16), rewritten in Go:

| | Python v16 | Go v18 |
|--|--|--|
| Loop interval | 2s | **200ms** |
| Price feed | REST ~118ms | **WebSocket ~1ms** |
| OBI/TFI | Serial | **Parallel goroutines** |
| Buy fee | Taker 0.05% | **LIMIT_MAKER 0%** |
| Sell fee | Taker/Maker | **LIMIT_MAKER 0%** |
| Storage | File I/O per loop | **In-memory + async** |
| Deploy | Python + venv | **Single binary** |

---

## Architecture

```
cmd/sol/main.go     # SOL/USDT
cmd/eth/main.go     # ETH/USDT
internal/bot/runner.go   # Main loop 200ms
internal/bot/engine.go  # CrashGuard, OBI/TFI
internal/bot/orders.go  # Sell fills, placement
internal/bot/batch.go   # In-memory + async disk
internal/mexc/client.go # REST API
internal/mexc/feed.go   # WebSocket feed
```

---

## Strategy

### Multi-Layer Grid

Spacing **0.10%**, 3 layers:

- Layer 0: anchor x (1 - 0.10%)
- Layer 1: anchor x (1 - 0.20%)
- Layer 2: anchor x (1 - 0.30%)

Buy: `LIMIT_MAKER` 0% fee. Sell: `LIMIT_MAKER` at +0.10%.
Net profit: **~0.10% per round trip**.

### Rolling Anchor

Anchor = sell price after each fill. Next buy = sell_price x 0.999.

### Zone Sizing

LOW (bottom 25%): 1.8x | MID (60%): 1.0x | HIGH (top 15%): 0.3x

### Indicators

- OBI: Order Book Imbalance, goroutine fetch every 4s
- TFI: Trade Flow Imbalance, last 50 trades
- CrashGuard: pauses buys on sharp drop

---

## Setup

```bash
git clone https://github.com/panyuti-ai/mexc-grid-bot.git
cd mexc-grid-bot
go build -o sol-bot ./cmd/sol/
go build -o eth-bot ./cmd/eth/
export MEXC_API_KEY=your_key
export MEXC_SECRET_KEY=your_secret
./sol-bot
```

Run each bot in a screen session on your VPS.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| MEXC_API_KEY | Yes | API key |
| MEXC_SECRET_KEY | Yes | Secret key |
| MEXC_DISCORD_WEBHOOK_URL | No | Discord alerts |
| BOT_DIR | No | Data dir for dashboard |

---

## License

MIT
