"""
trigger.py - LANA Cron v8
流程：
1. /api/scan 取所有幣分數
2. 分數 >= MIN_SCORE 的幣 → 呼叫 /api/ai_analyze 深度分析
3. AI 說 LONG/SHORT 的訊號依 AI 評分排序，只推最強的前 TOP_N_PUSH 個（避免選擇困難）
4. AI 說 WATCH → 靜默跳過
5. 同一顆幣 4 小時冷卻（由 app.py 伺服器端記憶體處理，cron 端本身無狀態）
"""

import requests, os, sys, time, hashlib
from datetime import datetime, timezone, timedelta

SCAN_URL     = os.getenv("SCAN_URL", "https://web-production-7cdf9.up.railway.app/api/scan")
AI_URL       = os.getenv("AI_URL", "https://web-production-7cdf9.up.railway.app/api/ai_analyze")
BOT_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_SCORE    = int(os.getenv("MIN_SCORE", "72"))
TOP_N_PUSH   = int(os.getenv("TOP_N_PUSH", "3"))

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
        if not r.ok:
            print(f"TG 推送失敗 HTTP {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        print(f"TG 錯誤: {e}")
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

def ai_analyze(coin, price, change_24h):
    """呼叫 AI 深度分析，回傳 direction/score/summary 等"""
    try:
        r = requests.post(
            AI_URL,
            json={"symbol": coin, "price": price, "change_24h": change_24h},
            timeout=45
        )
        if r.ok:
            data = r.json()
            # ai_analyze 直接回傳 {direction, score, ...}，沒有 ok 包裝
            if data.get("error"):
                print(f"  AI 回傳錯誤 {coin}: {data.get('error')}")
                return None
            if data.get("direction"):
                return data
        else:
            print(f"  AI HTTP {r.status_code} {coin}")
    except Exception as e:
        print(f"  AI 分析失敗 {coin}: {e}")
    return None

def format_signal(coin, ai_result, scan_score, change, price, scan_coin=None):
    direction = ai_result.get("direction", "WATCH")
    score     = ai_result.get("score", scan_score)
    entry     = ai_result.get("entry_zone", "")
    sl        = ai_result.get("stop_loss", "")
    t1        = ai_result.get("target_1", "")
    t2        = ai_result.get("target_2", "")
    timeframe = ai_result.get("timeframe", "4-8小時")
    risk_note = ai_result.get("risk_note", "嚴控倉位，設好止損，單筆不超 3-5%")
    summary   = ai_result.get("summary", "")
    reason    = ai_result.get("reason", "")
    sc        = scan_coin or {}
    rsi       = ai_result.get("rsi_1h") or ai_result.get("rsi") or sc.get("rsi") or ""
    vol_ratio = ai_result.get("vol_ratio") or sc.get("vol_ratio") or ""
    funding   = ai_result.get("funding_rate") or sc.get("funding") or ""

    dir_emoji  = {"LONG": "🟢", "SHORT": "🔴"}.get(direction, "⚪")
    dir_text   = {"LONG": "做多 ▲", "SHORT": "做空 ▼"}.get(direction, "觀望")
    conf_label = "高 🔥" if score >= 80 else "中 ✅" if score >= 65 else "低 ⚠️"
    now_str    = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")

    rsi_str = f"{rsi:.1f}" if isinstance(rsi, float) else str(rsi) if rsi else "N/A"
    vr_str  = f"{vol_ratio:.1f}x" if isinstance(vol_ratio, float) else str(vol_ratio) if vol_ratio else "N/A"
    fr_str  = f"{funding:+.3%}" if isinstance(funding, float) else "N/A"

    lines = [
        f"{dir_emoji} <b>{coin}/USDT (OKX)</b>",
        f"現價: {price}  📈 24h {change:+.1f}%",
        f"方向: {dir_text}  🔥 信心: {conf_label}  訊號強度: {score}/100",
        f"RSI 1H: {rsi_str}  量能: {vr_str}  FR: {fr_str}",
    ]
    if summary:
        lines.append(f"\n📌 {summary}")
    if reason:
        lines.append(f"<i>{reason}</i>")
    if direction in ("LONG", "SHORT") and entry:
        lines += [
            "",
            f"🎯 入場區間: {entry}",
            f"🔴 止損: {sl}",
            f"✅ 目標1: {t1}",
            f"🏆 目標2: {t2}",
            f"⏱ 預期持倉: {timeframe}",
            f"⚠️ {risk_note}",
        ]
    lines.append(f"\n⏰ {now_str}")
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

    # 分數 >= MIN_SCORE 的候選
    candidates = [c for c in coins if (c.get("lana_score") or 0) >= MIN_SCORE]
    print(f"  候選 (>={MIN_SCORE}分): {len(candidates)} 顆")

    # TG 指紋去重
    recent_fps = tg_recent_fingerprints()

    qualified = []  # 通過AI分析且判定LONG/SHORT的訊號，先收集起來排序
    for c in candidates:
        coin   = c["coin"]
        score  = c.get("lana_score", 0)
        change = c.get("change", 0)
        price  = c.get("price", 0)

        # 冷卻由 app.py 伺服器端記憶體處理（cron 端每次都是全新容器，本地無法持久記憶冷卻）

        print(f"  AI 分析 {coin} (scan:{score}分)...")
        ai_result = ai_analyze(coin, price, change)

        if not ai_result:
            print(f"  {coin} AI 分析失敗，跳過")
            continue

        direction = ai_result.get("direction", "WATCH")
        ai_score  = ai_result.get("score", 0)
        print(f"  {coin} AI結果: {direction} {ai_score}分")

        if direction not in ("LONG", "SHORT"):
            print(f"  {coin} AI說{direction}，跳過")
            continue

        qualified.append({
            "coin": coin, "ai_result": ai_result, "ai_score": ai_score,
            "score": score, "change": change, "price": price, "scan_coin": c
        })

    # 依AI評分排序，只推最強的前 TOP_N_PUSH 個，避免一次丟太多訊號造成選擇困難
    qualified.sort(key=lambda x: x["ai_score"], reverse=True)
    to_send = qualified[:TOP_N_PUSH]
    skipped_n = len(qualified) - len(to_send)

    pushed = 0
    for q in to_send:
        coin = q["coin"]
        msg = format_signal(coin, q["ai_result"], q["score"], q["change"], q["price"], scan_coin=q["scan_coin"])

        # 指紋去重
        fp = hashlib.md5(msg[:80].encode()).hexdigest()[:8]
        if fp in recent_fps:
            print(f"  {coin} 10分鐘內已推送，跳過")
            continue

        ok = send_tg(msg)
        if ok:
            recent_fps.add(fp)
            pushed += 1
            print(f"  ✅ {coin} 推送完成")
        else:
            print(f"  ❌ {coin} 推送失敗（未列入計數，指紋也不記錄，避免之後被誤判已送過）")
        time.sleep(1.5)

    if skipped_n > 0:
        skipped_coins = ", ".join(q["coin"] for q in qualified[TOP_N_PUSH:])
        print(f"  共{len(qualified)}個訊號達標，只推最強{len(to_send)}個，略過{skipped_n}個（{skipped_coins}）")

    # 掃描彙報
    top5 = sorted(coins, key=lambda x: x.get("lana_score") or 0, reverse=True)[:5]
    lines = ["🔍 LANA 掃描結果:", ""]
    for c in top5:
        sc = c.get("lana_score", "N/A")
        ch = c.get("change", 0)
        lines.append(f"💰 {c['coin']}: {sc} 分 | 漲幅 {ch:+.1f}%")
    lines.append(f"\n⏰ {ts}  |  AI分析: {len(candidates)} 顆候選  |  達標: {len(qualified)} 個  |  推送: {pushed} 個")
    send_tg("\n".join(lines))

    print(f"完成，推送 {pushed} 個")

except Exception as e:
    print(f"錯誤: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)
