"""
trigger.py - LANA Cron 觸發器
每次執行：
1. 呼叫 /api/scan 取得所有幣的分數
2. 對分數 >= MIN_SCORE 的幣，呼叫 /api/analysis 取得詳細分析
3. 推送完整訊號格式到 Telegram（含入場區間、止損、目標價）
4. 每輪結束後推送「掃描彙報」（顯示前5名分數）
"""

import requests, os, sys, time, hashlib
from datetime import datetime, timezone, timedelta

SCAN_URL     = os.getenv("SCAN_URL", "https://web-production-7cdf9.up.railway.app/api/scan")
ANALYSIS_URL = os.getenv("ANALYSIS_URL", "https://web-production-7cdf9.up.railway.app/api/analyze")
BOT_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_SCORE    = int(os.getenv("MIN_SCORE", "76"))

TZ_TAIPEI = timezone(timedelta(hours=8))

# ── 去重：同一顆幣 2 小時內不重複推送 ──
_COOLDOWN_FILE = "/tmp/lana_alerted.txt"

def _load_alerted():
    try:
        alerted = {}
        with open(_COOLDOWN_FILE) as f:
            for line in f:
                line = line.strip()
                if ":" in line:
                    coin, ts = line.split(":", 1)
                    alerted[coin] = float(ts)
        return alerted
    except:
        return {}

def _save_alerted(alerted):
    try:
        with open(_COOLDOWN_FILE, "w") as f:
            for coin, ts in alerted.items():
                f.write(f"{coin}:{ts}\n")
    except:
        pass

def send_tg(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ TG 憑證缺失")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        if not r.ok:
            print(f"TG 推送失敗: {r.text}")
    except Exception as e:
        print(f"TG 錯誤: {e}")

def format_signal(analysis, coin, score, change, price):
    """格式化完整訊號（對齊 meme-scanner 格式）"""
    direction = analysis.get("direction", "WATCH")
    entry     = analysis.get("entry_zone", "")
    sl        = analysis.get("stop_loss", "")
    t1        = analysis.get("target_1", "")
    t2        = analysis.get("target_2", "")
    timeframe = analysis.get("timeframe", "4-8小時")
    risk_note = analysis.get("risk_note", "嚴控倉位，設好止損")
    reason    = analysis.get("reason", "")
    rsi       = analysis.get("rsi", "")
    vol_ratio = analysis.get("vol_ratio", "")
    funding   = analysis.get("funding_rate", "")

    dir_emoji = {"LONG": "🟢", "SHORT": "🔴", "WATCH": "⚪"}.get(direction, "⚪")
    dir_text  = {"LONG": "做多 ▲", "SHORT": "做空 ▼", "WATCH": "觀望"}.get(direction, "觀望")

    conf = "高" if score >= 80 else "中" if score >= 65 else "低"
    conf_label = f"高 🔥" if score >= 80 else f"中 ✅" if score >= 65 else f"低 ⚠️"

    now_str = datetime.now(TZ_TAIPEI).strftime("%H:%M")

    msg = f"""{dir_emoji} <b>{coin}/USDT (OKX)</b>
現價: {price}  📈 24h {change:+.1f}%
方向: {dir_text}  🔥 信心: {conf_label}  訊號強度: {score}/100
RSI 1H: {rsi or 'N/A'}  量能: {f'{vol_ratio:.1f}x' if vol_ratio else 'N/A'}  FR: {f'{funding:+.3%}' if funding else 'N/A'}"""

    if reason:
        msg += f"\n\n📌 {reason}"

    if direction in ("LONG", "SHORT") and entry:
        msg += f"""

🎯 入場區間: {entry}
🔴 止損: {sl}
✅ 目標1: {t1}
🏆 目標2: {t2}
⏱ 預期持倉: {timeframe}
⚠️ {risk_note}"""

    msg += f"\n\n⏰ {now_str}"
    return msg

def get_analysis(coin, price):
    """呼叫 /api/analyze/<coin> 取得詳細分析"""
    try:
        r = requests.get(
            f"{ANALYSIS_URL}/{coin}",
            timeout=45
        )
        if r.ok:
            data = r.json()
            if data.get("ok"):
                return data.get("data") or data.get("analysis") or data
    except Exception as e:
        print(f"  analysis 失敗 {coin}: {e}")
    return None

# ── 主流程 ──────────────────────────────────────────────────
try:
    print(f"[{datetime.now(TZ_TAIPEI).strftime('%H:%M')}] 開始掃描...")
    r = requests.get(SCAN_URL, timeout=45)
    data = r.json()

    if not data.get("ok"):
        print(f"掃描 API 失敗: {data.get('error')}")
        sys.exit(1)

    coins = data.get("data", [])
    ts    = data.get("ts", "")
    print(f"  取得 {len(coins)} 顆幣")

    # 分數 >= MIN_SCORE 的候選
    candidates = [
        c for c in coins
        if (c.get("lana_score") or 0) >= MIN_SCORE
    ]
    print(f"  達標 (>={MIN_SCORE}分) 幣數: {len(candidates)}")

    # 去重
    alerted = _load_alerted()
    now_ts  = time.time()
    COOLDOWN = 2 * 3600  # 2小時冷卻

    pushed = 0
    for c in candidates:
        coin  = c["coin"]
        score = c.get("lana_score", 0)
        change= c.get("change", 0)
        price = c.get("price", 0)

        last = alerted.get(coin, 0)
        if now_ts - last < COOLDOWN:
            print(f"  {coin} 冷卻中，跳過")
            continue

        print(f"  推送 {coin} ({score}分)...")
        analysis = get_analysis(coin, price)

        if analysis:
            msg = format_signal(analysis, coin, score, change, price)
        else:
            # fallback：無法取得詳細分析，推簡易格式
            msg = f"🟡 <b>{coin}/USDT</b>\n分數: {score}/100 | 24h: {change:+.1f}%\n現價: {price}\n⏰ {ts}"

        send_tg(msg)
        alerted[coin] = now_ts
        pushed += 1
        time.sleep(1)  # 避免 TG rate limit

    _save_alerted(alerted)

    # ── 每輪掃描彙報（前5名）──
    top5 = sorted(coins, key=lambda x: x.get("lana_score") or 0, reverse=True)[:5]
    summary = "🔍 LANA 掃描結果:\n\n"
    for c in top5:
        sc = c.get("lana_score", "N/A")
        ch = c.get("change", 0)
        summary += f"💰 {c['coin']}: {sc} 分 | 漲幅 {ch:+.1f}%\n"
    summary += f"\n⏰ {ts}  |  推送訊號: {pushed} 個"
    send_tg(summary)

    print(f"完成，推送 {pushed} 個訊號")

except Exception as e:
    print(f"錯誤: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)
