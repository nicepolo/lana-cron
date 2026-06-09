import requests, os, sys

url = os.getenv("SCAN_URL", "https://tender-laughter-production-5045.up.railway.app/api/trigger_scan")
try:
    r = requests.post(url, timeout=30)
    print(f"觸發成功: {r.status_code} {r.text}")
except Exception as e:
    print(f"觸發失敗: {e}")
    sys.exit(1)
