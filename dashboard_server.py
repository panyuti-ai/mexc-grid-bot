"""
Jane Street Bot Dashboard API Server v2
在伺服器上跑：python3 dashboard_server.py
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import json, os, requests
from datetime import datetime

app = Flask(__name__)
CORS(app)

BOT_DIR = os.path.expanduser("~/AI-trading-bot")
COINS = ["eth", "sol", "xrp"]
SYMBOL_MAP = {"eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
MEXC_BASE = "https://api.mexc.com"

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {}

def load_list(path):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return []

@app.route("/api/stats")
def stats():
    result = {}
    total_profit = 0.0
    total_trades = 0
    for coin in COINS:
        s = load_json(f"{BOT_DIR}/mexc_stats_{coin}.json")
        daily = s.get("daily", {})
        result[coin] = {
            "total_profit": round(s.get("total_profit", 0), 4),
            "total_trades": s.get("total_trades", 0),
            "daily": {
                date: {
                    "trades": d.get("trades", 0),
                    "profit": round(d.get("profit", 0), 4),
                    "inner": round(d.get("inner", 0), 4),
                    "outer": round(d.get("outer", 0), 4),
                }
                for date, d in sorted(daily.items())
            }
        }
        total_profit += s.get("total_profit", 0)
        total_trades += s.get("total_trades", 0)
    result["summary"] = {
        "total_profit": round(total_profit, 4),
        "total_trades": total_trades,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    return jsonify(result)


TRADES_LOG_FILE = os.path.join(BOT_DIR, "trades_log.json")

@app.route("/api/trades")
def trades():
    try:
        with open(TRADES_LOG_FILE) as f:
            data = json.load(f)
        limit = int(request.args.get("limit", 200))
        return jsonify(list(reversed(data))[:limit])
    except:
        return jsonify([])

@app.route("/api/batches")
def batches():
    result = {}
    for coin in COINS:
        result[coin] = load_list(f"{BOT_DIR}/mexc_batches_{coin}.json")
    return jsonify(result)

@app.route("/api/all/<coin>")
def all_data(coin):
    symbol = SYMBOL_MAP.get(coin.lower())
    if not symbol:
        return jsonify({"error": "unknown coin"}), 400

    price_val = 0.0
    try:
        r = requests.get(f"{MEXC_BASE}/api/v3/ticker/price", params={"symbol": symbol}, timeout=5)
        price_val = float(r.json().get("price", 0))
    except:
        pass

    candles = []
    try:
        interval = request.args.get("interval", "15m")
        r = requests.get(
            f"{MEXC_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": 150},
            timeout=10
        )
        for c in r.json():
            candles.append({
                "time": int(c[0]) // 1000,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })
    except:
        pass

    return jsonify({
        "coin": coin,
        "symbol": symbol,
        "price": price_val,
        "candles": candles,
        "batches": load_list(f"{BOT_DIR}/mexc_batches_{coin}.json"),
        "stats": load_json(f"{BOT_DIR}/mexc_stats_{coin}.json"),
        "metrics": load_json(f"{BOT_DIR}/mexc_metrics_{coin}.json"),
    })

@app.route("/")
@app.route("/dashboard")
def dashboard():
    from flask import send_file
    html_path = os.path.join(BOT_DIR, "dashboard.html")
    if os.path.exists(html_path):
        return send_file(html_path)
    return "dashboard.html 不存在", 404

@app.route("/lw-charts.js")
def lw_charts():
    from flask import send_file
    return send_file(os.path.join(BOT_DIR, "lw-charts.js"), mimetype="application/javascript")

if __name__ == "__main__":
    print("🚀 Dashboard API v2 啟動於 http://0.0.0.0:5566")
    app.run(host="0.0.0.0", port=5566, debug=False)
