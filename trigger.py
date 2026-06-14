import requests, os, sys

url = os.getenv("SCAN_URL", "https://web-production-7cdf9.up.railway.app/api/scan")
telegram_token = os.getenv("TELEGRAM_TOKEN")
telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

try:
    r = requests.get(url, timeout=30)
    data = r.json()
    
    if data.get("ok"):
        coins = data.get("data", [])[:5]  # 前 5 個幣
        msg = "🔍 LANA 掃描結果:\n\n"
        for coin in coins:
            score = coin.get("lana_score", "N/A")
            change = coin.get("change", "N/A")
            msg += f"💰 {coin['coin']}: {score} 分 | 漲幅 {change}%\n"
        msg += f"\n⏰ {data.get('ts', 'N/A')}"
        
        # 發送 TG
        tg_url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        requests.post(tg_url, json={"chat_id": telegram_chat_id, "text": msg})
        print(f"掃描成功並發送 TG 通知")
    else:
        print(f"掃描失敗: {data.get('error')}")
        
except Exception as e:
    print(f"錯誤: {e}")
    sys.exit(1)
