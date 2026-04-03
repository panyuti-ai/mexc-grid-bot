# MEXC Grid Trading Bot — Jane Street v17

An automated cryptocurrency grid trading bot running on MEXC exchange.
Currently trading ETH/USDT and SOL/USDT with real capital on a Vultr VPS (Singapore).

---

## Why rewrite from Python to Go?

The original bot was written in Python (`mexc_base.py`). It worked, but had a few fundamental limitations:

**Python version (v16)**
- Two bots (ETH + SOL) ran as separate processes. Because of Python's GIL, they couldn't truly share CPU time — each process competed for resources.
- The sell order check interval was 60 seconds. Meaning after a sell filled, the bot took up to 60 seconds to detect it, reset the anchor, and trigger the next buy cycle. High-frequency grid trading needs faster reaction.
- Required a Python virtualenv, `pymexc` SDK, and `flask` for the dashboard.

**Go version (v17)**
- Goroutines allow true concurrent API calls — OBI/TFI updates, order book queries, and sell checks run without blocking the main loop.
- Sell order check interval reduced to **10 seconds**. The anchor resets faster, the next buy triggers sooner.
- `auto_detect_capital` now correctly counts both USDT balance **and** coin holdings at market price — not just USDT. This fixes a bug where the first loop after restart would use a much smaller capital estimate.
- Anchor initialization uses **current price** (not last batch buy price). Prevents the bot from immediately triggering a buy the moment it restarts.
- Single static binary per coin — no virtualenv, no pip, no SDK dependency. Deploy by copying one file.
- Dashboard rewrites Flask in Go's standard `net/http` — no Python process needed.

---

## Architecture

```
go/
├── cmd/
│   ├── sol/main.go         # SOL/USDT entry point
│   ├── eth/main.go         # ETH/USDT entry point
│   ├── xrp/main.go         # XRP/USDT entry point
│   └── dashboard/main.go   # Dashboard HTTP server
└── internal/
    ├── bot/
    │   ├── runner.go       # Main strategy loop
    │   ├── engine.go       # MarketEngine, CrashGuard, BuyRateGuard, OFI cache
    │   ├── orders.go       # Order execution, sell fill detection
    │   ├── pricing.go      # Zone-based sizing, price calculations
    │   ├── batch.go        # Batch file management (JSON)
    │   └── stats.go        # Stats, metrics, trade log
    ├── mexc/client.go      # MEXC REST API client (HMAC signing, pure stdlib)
    ├── discord/discord.go  # Discord webhook sender
    ├── dotenv/dotenv.go    # .env file loader (no external deps)
    └── entry/entry.go      # Shared startup flow for all coins
```

Adding a new coin takes under 5 minutes — copy `cmd/xrp/main.go`, change the config values, run `go build`.

---

## Strategy

- **Grid spacing**: 0.20% per layer
- **Entry**: Taker market buy when price drops by grid spacing from anchor
- **Exit**: Maker limit sell above entry (MEXC Maker fee = 0%, net profit ≈ 0.15% per round trip)
- **Anchor reset**: After each sell, anchor resets to current price for the next cycle
- **Zone-based sizing**: LOW zone (bottom 25%) → 1.8x position, MID → 1.0x, HIGH (top 15%) → 0.3x
- **Crash Guard**: Pauses buying on sharp drops (OBI + TFI + 1-minute drop threshold)
- **Dynamic reset multiplier**: Near bottom → anchor resets faster; near top → slower

---

## Setup

```bash
# Clone
git clone https://github.com/panyuti-ai/mexc-grid-bot.git
cd mexc-grid-bot/go

# Build
go build -o mexc_sol ./cmd/sol/
go build -o mexc_eth ./cmd/eth/
go build -o mexc_dashboard ./cmd/dashboard/

# Configure
cp ../.env.example .env
# Fill in MEXC_API_KEY, MEXC_SECRET_KEY, MEXC_DISCORD_WEBHOOK_URL

# Run
./mexc_sol      # prompts for price range, then starts
./mexc_eth
BOT_DIR=$(pwd) ./mexc_dashboard   # http://your-vps:5566/dashboard
```

---

## Environment Variables

```
MEXC_API_KEY=your_key
MEXC_SECRET_KEY=your_secret
MEXC_DISCORD_WEBHOOK_URL=your_webhook
BOT_DIR=/path/to/json/files   # for dashboard only, defaults to ~/AI-trading-bot
```

---

## Deployment (Vultr VPS)

```bash
# Each coin runs in its own screen session
screen -S sol
./mexc_sol

screen -S eth
./mexc_eth

screen -S dashboard
BOT_DIR=~/bot-go-src ./mexc_dashboard
```

Data files (`mexc_batches_*.json`, `mexc_stats_*.json`, `trades_log.json`) are
compatible between the Python and Go versions — same format, same filenames.

---

## License

MIT
