"""
trigger.py - LANA Cron 觸發器 v3
核心邏輯：用 /api/analyze 結果決定推送，確保 TG 與網頁一致
1. /api/scan 只取幣種列表
2. 每顆幣跑 /api/analyze
3. analyze 說 LONG/SHORT 且分數 >= MIN_SCORE → 推送
4. analyze 說 WATCH → 跳過
"""

import requests, os, sys, time, hashlib
from datetime import datetime, timezone, timedelta

SCAN_URL     = os.getenv("SCAN_URL", "https://web-production-7cdf9.up.railway.app/api/scan")
ANALYSIS_URL = os.getenv("ANALYSIS_URL", "https://web-production-7cdf9.up.railway.app/api/analyze")
BOT_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_SCORE    = int(os.getenv("MIN_SCORE", "65"))

TZ_TAIPEI = timezone(timedelta(hours=8))

def send_tg(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ TG 憑證缺失")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        return r.ok
    except Exception as e:
        print(f"TG 錯誤: {e}")
        return False

def tg_recent_fingerprints():
    """取得 TG 最近 10 分鐘訊息指紋，跨 container 去重"""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"limit": 20, "offset": -20},
            timeout=8
        )
        if not r.ok:
            return set()
        updates = r.json().get("result", [])
        fps = set()
        now_ts = time.time()
        for u in updates:
            msg = u.get("message", {}) or u.get("channel_post", {})
            msg_ts = msg.get("date", 0)
            text = msg.get("text", "")
            if now_ts - msg_ts < 600 and text:
                fps.add(hashlib.md5(text[:80].encode()).hexdigest()[:8])
        return fps
    except:
        return set()

def get_analysis(coin):
    try:
        r = requests.get(f"{ANALYSIS_URL}/{coin}", timeout=45)
        if r.ok:
            data = r.json()
            if data.get("ok"):
                return data.get("data") or data.get("analysis")
    except Exception as e:
        print(f"  analyze 失敗 {coin}: {e}")
    return None

def format_signal(analysis, coin, change, price):
    score     = analysis.get("lana_score") or analysis.get("score") or 0
    direction = analysis.get("direction", "WATCH")
    entry     = analysis.get("entry_zone", "")
    sl        = analysis.get("stop_loss", "")
    t1        = analysis.get("target_1", "")
    t2        = analysis.get("target_2", "")
    timeframe = analysis.get("timeframe", "4-8小時")
    risk_note = analysis.get("risk_note", "嚴控倉位，設好止損")
    reason    = analysis.get("reason", "")
    rsi       = analysis.get("rsi") or analysis.get("rsi_1h", "")
    vol_ratio = analysis.get("vol_ratio") or analysis.get("vol_ratio_1h", "")
    funding   = analysis.get("funding_rate", "")

    dir_emoji  = {"LONG": "🟢", "SHORT": "🔴"}.get(direction, "⚪")
    dir_text   = {"LONG": "做多 ▲", "SHORT": "做空 ▼"}.get(direction, "觀望")
    conf_label = "高 🔥" if score >= 80 else "中 ✅" if score >= 65 else "低 ⚠️"
    now_str    = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")

    msg = f"""{dir_emoji} <b>{coin}/USDT (OKX)</b>
現價: {price}  📈 24h {change:+.1f}%
方向: {dir_text}  🔥 信心: {conf_label}  訊號強度: {score}/100
RSI 1H: {rsi or 'N/A'}  量能: {f'{vol_ratio:.1f}x' if vol_ratio else 'N/A'}  FR: {f'{funding:+.3%}' if funding else 'N/A'}"""

    if reason:
        msg += f"\n\n📌 {reason}"

    if entry:
        msg += f"""

🎯 入場區間: {entry}
🔴 止損: {sl}
✅ 目標1: {t1}
🏆 目標2: {t2}
⏱ 預期持倉: {timeframe}
⚠️ {risk_note}"""

    msg += f"\n\n⏰ {now_str}"
    return msg

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

    # 取近期 TG 指紋去重
    recent_fps = tg_recent_fingerprints()

    pushed   = 0
    skipped  = 0

    for c in coins:
        coin   = c["coin"]
        change = c.get("change", 0)
        price  = c.get("price", 0)

        analysis = get_analysis(coin)
        if not analysis:
            continue

        direction = analysis.get("direction", "WATCH")
        score     = analysis.get("lana_score") or analysis.get("score") or 0

        # 只推 LONG/SHORT 且分數達標
        raw_keys = list(analysis.keys()) if analysis else []
        print(f"  {coin}: direction={direction} score={score} keys={raw_keys}")
        if direction == "WATCH":
            skipped += 1
            continue
        if score < MIN_SCORE:
            print(f"  {coin} {direction} {score}分 < {MIN_SCORE}，跳過")
            skipped += 1
            continue

        msg = format_signal(analysis, coin, change, price)

        # 指紋去重
        fp = hashlib.md5(msg[:80].encode()).hexdigest()[:8]
        if fp in recent_fps:
            print(f"  {coin} 10分鐘內已推送，跳過")
            continue

        print(f"  ✅ 推送 {coin} {direction} {score}分")
        send_tg(msg)
        recent_fps.add(fp)
        pushed += 1
        time.sleep(1.5)

    # 掃描彙報（前5名用 scan 分數排序）
    top5 = sorted(coins, key=lambda x: x.get("lana_score") or 0, reverse=True)[:5]
    summary = "🔍 LANA 掃描結果:\n\n"
    for c in top5:
        sc = c.get("lana_score", "N/A")
        ch = c.get("change", 0)
        summary += f"💰 {c['coin']}: {sc} 分 | 漲幅 {ch:+.1f}%\n"
    summary += f"\n⏰ {ts}  |  推送訊號: {pushed} 個"
    send_tg(summary)

    print(f"完成，推送 {pushed} 個，跳過 {skipped} 個")

except Exception as e:
    print(f"錯誤: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)
