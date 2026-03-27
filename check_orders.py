from dotenv import load_dotenv
import os
os.chdir('/root/AI-trading-bot')
load_dotenv()
import okx.Trade as Trade

api = Trade.TradeAPI(os.environ['OKX_API_KEY'], os.environ['OKX_SECRET_KEY'], os.environ['OKX_PASSPHRASE'], flag='0')
result = api.get_order_list(instType='SPOT')
f = open('/tmp/orders_result.txt', 'w')
f.write(f"code: {result.get('code')}\n")
f.write(f"data count: {len(result.get('data', []))}\n")
for o in result.get('data', []):
    f.write(f"  {o['instId']} ordId={o['ordId']} px={o['px']} sz={o['sz']} side={o['side']}\n")
f.close()
