from dotenv import load_dotenv
load_dotenv('/root/AI-trading-bot/.env')
import okx.Trade as Trade
import okx.Account as Account
import os, json

api = Trade.TradeAPI(os.environ['OKX_API_KEY'], os.environ['OKX_SECRET_KEY'], os.environ['OKX_PASSPHRASE'], flag='0')
acc = Account.AccountAPI(os.environ['OKX_API_KEY'], os.environ['OKX_SECRET_KEY'], os.environ['OKX_PASSPHRASE'], flag='0')

# 帳戶餘額
details = acc.get_account_balance()['data'][0]['details']
print("═" * 60)
print("  💰 帳戶餘額")
print("═" * 60)
for item in details:
    if float(item.get('cashBal', 0)) > 0:
        print(f"  {item['ccy']}: 可用={item['availBal']}  總額={item.get('cashBal', item['availBal'])}")

# OKX 活著的賣單
print("\n" + "═" * 60)
print("  📋 OKX 上的活躍訂單")
print("═" * 60)
for sym in ['ETH-USDT', 'SOL-USDT']:
    orders = api.get_order_list(instType='SPOT', instId=sym).get('data', [])
    print(f"\n  {sym}: {len(orders)} 筆")
    for o in orders:
        print(f"    #{o['ordId'][-8:]} {o['side']} px=${o['px']} sz={o['sz']} state={o['state']}")

# JSON 批次
print("\n" + "═" * 60)
print("  📦 Bot 追蹤的批次")
print("═" * 60)
for f in ['batches_eth.json', 'batches_sol.json']:
    try:
        d = json.load(open(f))
        print(f"\n  {f}: {len(d)} 批")
        for b in d:
            sell = f"賣:${b['sell_price']:.2f}" if b.get('sell_price') else "賣:無"
            oid = "有掛單" if b.get('sell_order_id') else "❌無掛單"
            print(f"    #{b['id']} {b['layer']} 買:${b['buy_price']:.2f} {sell} {oid}")
    except:
        print(f"\n  {f}: 空或不存在")

# 比對
print("\n" + "═" * 60)
print("  🔍 比對結果")
print("═" * 60)
for sym, f in [('ETH-USDT', 'batches_eth.json'), ('SOL-USDT', 'batches_sol.json')]:
    orders = api.get_order_list(instType='SPOT', instId=sym).get('data', [])
    okx_ids = {o['ordId'] for o in orders}
    try:
        batches = json.load(open(f))
    except:
        batches = []
    bot_ids = {b['sell_order_id'] for b in batches if b.get('sell_order_id')}
    orphan_okx = okx_ids - bot_ids
    orphan_bot = bot_ids - okx_ids
    if orphan_okx:
        print(f"  ⚠️ {sym} OKX有但Bot不知道: {len(orphan_okx)}筆")
    if orphan_bot:
        print(f"  ⚠️ {sym} Bot有但OKX沒有: {len(orphan_bot)}筆")
    if not orphan_okx and not orphan_bot:
        print(f"  ✅ {sym} 完全同步")
