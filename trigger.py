import requests, os, sys

url = os.getenv("SCAN_URL", "https://web-production-7cdf9.up.railway.app/api/scan")

try:
    r = requests.get(url, timeout=30)
    print(f"掃描成功: {r.status_code} {r.text[:200]}")
except Exception as e:
    print(f"掃描失敗: {e}")
    sys.exit(1)
