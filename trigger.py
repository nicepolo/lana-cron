"""
trigger.py - LANA Cron v4
直接用 /api/scan 結果判斷方向
條件：lana_score >= MIN_SCORE AND ma_bull=True AND rsi < 75
"""

import requests, os, sys, time, hashlib
from datetime import datetime, timezone, timedelta

SCAN_URL  = os.getenv("SCAN_URL", "https://web-production-7cdf9.up.railway.app/api/scan")
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_SCORE = int(os.getenv("MIN_SCORE", "65"))

TZ_TAIPEI = timezone(timedelta(hours=8))

def send_tg(text):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        return r.ok
    except Exception as e:
        print(f"TG error: {e}")
        return False

def tg_recent_fingerprints():
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"limit": 20, "offset": -20},
            timeout=8
        )
        if not r.ok:
            return set()
        fps = set()
        now_ts = time.time()
        for u in r.json().get("result", []):
            msg = u.get("message", {}) or u.get("channel_post", {})
            if now_ts - msg.get("date", 0) < 600:
                text = msg.get("text", "")
                if text:
                    fps.add(hashlib.md5(text[:80].encode()).hexdigest()[:8])
        return fps
    except:
        return set()

def format_signal(coin, score, change, price, rsi, vol_ratio, funding, bb_pos):
    conf_label = "高 🔥" if score >= 80 else "中 ✅" if score >= 65 else "低 ⚠️"
    now_str    = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")

    entry_lo = round(price * 0.995, 4)
    entry_hi = round(price * 1.002, 4)
    sl       = round(price * 0.97, 4)
    t1       = round(price * 1.04, 4)
    t2       = round(price * 1.08, 4)

    rsi_str = f"{rsi:.1f}" if rsi else "N/A"
    vr_str  = f"{vol_ratio:.1f}x" if vol_ratio else "N/A"
    fr_str  = f"{funding:+.3%}" if funding else "N/A"

    bb_map = {
        "lower_half": "布林下軌支撐",
        "below_lower": "突破下軌超賣",
        "upper_half": "布林中軌上方",
        "above_upper": "突破上軌"
    }
    bb_txt  = bb_map.get(bb_pos, "")
    rsi_note = ("RSI超賣有反彈機會" if rsi and rsi < 35 else
                "RSI偏弱有反彈機會" if rsi and rsi < 50 else
                "RSI中性" if rsi and rsi < 60 else "RSI偏強")

    lines = [
        f"🟢 <b>{coin}/USDT (OKX)</b>",
        f"現價: {price}  📈 24h {change:+.1f}%",
        f"方向: 做多 ▲  🔥 信心: {conf_label}  訊號強度: {score}/100",
        f"RSI 1H: {rsi_str}  量能: {vr_str}  FR: {fr_str}",
        "",
        f"📌 MA三線多頭排列，{rsi_note}，{bb_txt}",
        "",
        f"🎯 入場區間: {entry_lo}-{entry_hi}",
        f"🔴 止損: {sl}",
        f"✅ 目標1: {t1}",
        f"🏆 目標2: {t2}",
        "⏱ 預期持倉: 4-8小時",
        "⚠️ 嚴控倉位，設好止損，單筆不超 3-5%",
        "",
        f"⏰ {now_str}"
    ]
    return "\n".join(lines)

# ── 主流程 ──────────────────────────────────────────────────
try:
    now_str = datetime.now(TZ_TAIPEI).strftime("%H:%M")
    print(f"[{now_str}] 開始掃描...")
    r = requests.get(SCAN_URL, timeout=45)
    data = r.json()

    if not data.get("ok"):
        print(f"掃描失敗: {data.get('error')}")
        sys.exit(1)

    coins = data.get("data", [])
    ts    = data.get("ts", "")
    print(f"  取得 {len(coins)} 顆幣")

    recent_fps = tg_recent_fingerprints()
    pushed = 0

    for c in coins:
        coin      = c["coin"]
        score     = c.get("lana_score") or 0
        change    = c.get("change", 0)
        price     = c.get("price", 0)
        ma_bull   = c.get("ma_bull", False)
        rsi       = c.get("rsi") or c.get("rsi_1h")
        vol_ratio = c.get("vol_ratio") or c.get("vol_ratio_1h")
        funding   = c.get("funding_rate", 0)
        bb_pos    = c.get("bb_position", "")

        if score >= MIN_SCORE and ma_bull and (rsi is None or rsi < 75):
            direction = "LONG"
        else:
            print(f"  {coin}: score={score} ma_bull={ma_bull} rsi={rsi} SKIP")
            continue

        print(f"  PUSH {coin}: LONG {score}pts")
        msg = format_signal(coin, score, change, price, rsi, vol_ratio, funding, bb_pos)

        fp = hashlib.md5(msg[:80].encode()).hexdigest()[:8]
        if fp in recent_fps:
            print(f"  {coin} recently sent, skip")
            continue

        send_tg(msg)
        recent_fps.add(fp)
        pushed += 1
        time.sleep(1.5)

    # 掃描彙報
    top5 = sorted(coins, key=lambda x: x.get("lana_score") or 0, reverse=True)[:5]
    lines = ["🔍 LANA 掃描結果:", ""]
    for c in top5:
        sc = c.get("lana_score", "N/A")
        ch = c.get("change", 0)
        lines.append(f"💰 {c['coin']}: {sc} 分 | 漲幅 {ch:+.1f}%")
    lines.append(f"\n⏰ {ts}  |  推送訊號: {pushed} 個")
    send_tg("\n".join(lines))

    print(f"完成，推送 {pushed} 個")

except Exception as e:
    print(f"錯誤: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)
