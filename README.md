# MEXC Grid Trading Bot

An automated cryptocurrency grid trading bot running on MEXC exchange, built with Python.

## Overview

A production-ready grid trading bot that executes automated buy/sell orders based on price grid strategies. Currently trading ETH/USDT and SOL/USDT pairs on MEXC with real capital.

## Features

- **Multi-coin support**: ETH, SOL, XRP (configurable)
- **Dynamic position sizing**: Three-zone capital allocation (High / Mid / Low price zones)
- **Smart anchor reset**: Post-sell anchor reset for high-frequency grid cycling
- **Crash protection**: Built-in crash guard and trend detection
- **Live dashboard**: Real-time web dashboard with candlestick charts, batch positions, and profit tracking
- **Discord notifications**: Real-time trade alerts via Discord webhook
- **Auto capital detection**: Dynamically adjusts position size based on available USDT balance

## Architecture
```
mexc_base.py          # Core bot engine (shared base class)
mexc_eth.py           # ETH/USDT configuration
mexc_sol.py           # SOL/USDT configuration
mexc_xrp.py           # XRP/USDT configuration
dashboard_server.py   # Flask API server
dashboard.html        # Live trading dashboard
```

## Tech Stack

- Python 3
- Flask (dashboard API)
- MEXC Spot API
- Deployed on Vultr VPS (Singapore)

## Setup
```bash
git clone https://github.com/panyuti-ai/mexc-grid-bot.git
cd mexc-grid-bot
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
python3 mexc_sol.py
```

## Environment Variables
```
MEXC_API_KEY=your_mexc_api_key
MEXC_SECRET_KEY=your_mexc_secret_key
MEXC_DISCORD_WEBHOOK_URL=your_discord_webhook
```

## Strategy

- **Grid spacing**: 0.20% per layer
- **Entry**: Taker market buy when price drops by grid spacing
- **Exit**: Maker limit sell above entry price
- **Anchor reset**: After each sell, anchor resets to current price for next cycle
- **Zone-based sizing**: Larger positions in low price zones, smaller in high zones
- **Trend protection**: Automatically widens grid spacing during downtrends

## License

MIT
