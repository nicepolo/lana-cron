"""
trigger.py - LANA Cron v9
流程：
1. /api/scan 取所有幣分數
2. 分數 >= MIN_SCORE 的幣 → 呼叫 /api/ai_analyze 深度分析
3. AI 評分 < PUSH_MIN_AI_SCORE 的訊號直接排除（不因候選不足硬推弱訊號）
4. AI 說 LONG/SHORT 且評分達標的訊號依評分排序，只推最強的前 TOP_N_PUSH 個
5. AI 說 WATCH → 靜默跳過
6. 同一顆幣 4 小時冷卻（由 app.py 伺服器端記憶體處理，cron 端本身無狀態）
"""

"""
trigger.py - LANA Cron v10
流程：
1. 處理 TG 指令（/pause /resume /status）
2. 檢查手動暫停開關 → 若暫停中則跳過本輪（省 API 費用）
3. /api/scan 取所有幣分數
4. 分數 >= MIN_SCORE 的幣 → 呼叫 /api/ai_analyze 深度分析
5. AI 評分 < PUSH_MIN_AI_SCORE 的訊號直接排除
6. AI 說 LONG/SHORT 且評分達標的訊號依評分排序，只推最強的前 TOP_N_PUSH 個
7. AI 說 WATCH → 靜默跳過
8. 同一顆幣 4 小時冷卻（由 app.py 伺服器端記憶體處理）
"""

import requests, os, sys, time, hashlib, html
from datetime import datetime, timezone, timedelta

SCAN_URL     = os.getenv("SCAN_URL", "https://web-production-7cdf9.up.railway.app/api/scan")
AI_URL       = os.getenv("AI_URL", "https://web-production-7cdf9.up.railway.app/api/ai_analyze")
CTRL_URL     = os.getenv("CTRL_URL", "https://web-production-7cdf9.up.railway.app/api/push_control")
BOT_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_SCORE         = int(os.getenv("MIN_SCORE", "72"))
TOP_N_PUSH        = int(os.getenv("TOP_N_PUSH", "3"))
PUSH_MIN_AI_SCORE = int(os.getenv("PUSH_MIN_AI_SCORE", "70"))

TZ_TAIPEI = timezone(timedelta(hours=8))

def send_tg(text, chat_id=None, reply_markup=None):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        payload = {"chat_id": chat_id or CHAT_ID, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
        if not r.ok:
            print(f"TG 推送失敗 HTTP {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        print(f"TG 錯誤: {e}")
        return False

def handle_tg_commands():
    """處理 TG 指令：/pause [小時]、/resume、/status"""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"limit": 10, "offset": -10},
            timeout=8
        )
        if not r.ok:
            return
        updates = r.json().get("result", [])
        now_ts = time.time()
        for u in updates:
            msg = u.get("message", {}) or u.get("channel_post", {})
            text = (msg.get("text") or "").strip()
            msg_ts = msg.get("date", 0)
            chat_id = str(msg.get("chat", {}).get("id", "") or CHAT_ID)
            # 只處理最近 2 分鐘內的訊息（避免重複處理舊指令）
            if now_ts - msg_ts > 120:
                continue
            if text.startswith("/pause"):
                parts = text.split()
                hours = 0
                if len(parts) > 1:
                    try: hours = float(parts[1])
                    except: pass
                payload = {"action": "pause", "hours": hours}
                resp = requests.post(CTRL_URL, json=payload, timeout=8)
                if resp.ok:
                    msg_out = resp.json().get("message", "已暫停")
                    send_tg(f"⏸ {msg_out}\n\n發送 /resume 可立即恢復推送", chat_id)
            elif text == "/resume":
                resp = requests.post(CTRL_URL, json={"action": "resume"}, timeout=8)
                if resp.ok:
                    send_tg("▶️ 已恢復推送訊號", chat_id)
            elif text == "/status":
                resp = requests.get(CTRL_URL, timeout=8)
                if resp.ok:
                    d = resp.json()
                    status = "🟢 推送中" if d.get("should_push") else f"⏸ {d.get('message', '暫停中')}"
                    send_tg(
                        f"📊 LANA 推送狀態\n\n{status}\n\n"
                        f"指令：\n/pause [小時] — 暫停（不填=永久）\n/resume — 恢復\n/status — 查狀態",
                        chat_id
                    )
    except Exception as e:
        print(f"處理TG指令失敗: {e}")

def check_should_push():
    """查詢伺服器端推送開關狀態"""
    try:
        r = requests.get(CTRL_URL, timeout=8)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"查詢推送狀態失敗: {e}，預設繼續推送")
    return {"should_push": True}

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
            print(f"  AI HTTP {r.status_code} {coin}: {r.text[:300]}")
    except Exception as e:
        print(f"  AI 分析失敗 {coin}: {e}")
    return None

def format_signal(coin, ai_result, scan_score, change, price, scan_coin=None):
    score     = ai_result.get("score", scan_score)
    sc        = scan_coin or {}
    rsi       = ai_result.get("rsi_1h") or ai_result.get("rsi") or sc.get("rsi") or ""
    vol_ratio = ai_result.get("vol_ratio") or sc.get("vol_ratio") or ""
    funding   = ai_result.get("funding_rate") or sc.get("funding") or ""
    summary   = html.escape(ai_result.get("summary", ""))
    reason    = html.escape(ai_result.get("reason", ""))

    conf_label = "高 🔥" if score >= 80 else "中 ✅" if score >= 65 else "低 ⚠️"
    now_str    = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")

    rsi_str = f"{rsi:.1f}" if isinstance(rsi, float) else str(rsi) if rsi else "N/A"
    vr_str  = f"{vol_ratio:.1f}x" if isinstance(vol_ratio, float) else str(vol_ratio) if vol_ratio else "N/A"
    fr_str  = f"{funding:+.3%}" if isinstance(funding, float) else "N/A"

    # 推播只顯示技術面,不給方向——方向交給使用者按「🔄 重新分析」決定
    lines = [
        f"📡 <b>{coin}/USDT (OKX)</b>",
        f"現價: {price}  📈 24h {change:+.1f}%",
        f"訊號強度: {score}/100  信心: {conf_label}",
        f"RSI 1H: {rsi_str}  量能: {vr_str}  FR: {fr_str}",
    ]
    if summary:
        lines.append(f"\n📌 {summary}")
    if reason:
        lines.append(f"<i>{reason}</i>")

    lines.append(f"\n⏰ {now_str}")
    return "\n".join(lines)


# ── 主流程 ──────────────────────────────────────────────────
try:
    now_str = datetime.now(TZ_TAIPEI).strftime("%H:%M")
    print(f"[{now_str}] 開始掃描...")

    # 先處理 TG 指令（/pause /resume /status）
    handle_tg_commands()

    # 查詢手動暫停開關
    ctrl = check_should_push()
    if not ctrl.get("should_push", True):
        msg = ctrl.get("message", "暫停中")
        print(f"  推送已暫停：{msg}，本輪跳過")
        sys.exit(0)

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
        ai_model  = ai_result.get("model", "?")
        print(f"  {coin} AI結果: {direction} {ai_score}分 [{ai_model}]")

        if direction not in ("LONG", "SHORT"):
            print(f"  {coin} AI說{direction}，跳過")
            continue

        if ai_score < PUSH_MIN_AI_SCORE:
            print(f"  {coin} AI評分{ai_score}低於推送門檻{PUSH_MIN_AI_SCORE}，跳過（不因候選不足而硬推弱訊號）")
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

        ok = send_tg(msg, reply_markup={
            "inline_keyboard": [[
                {"text": "🔄 重新分析", "callback_data": f"reanalyze:{coin}"},
                {"text": "⏸ 暫停4小時", "callback_data": "pause:4"}
            ]]
        })
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

    # 掃描彙報（帶完整控制按鈕，不用打指令）
    top5 = sorted(coins, key=lambda x: x.get("lana_score") or 0, reverse=True)[:5]
    lines = ["🔍 LANA 掃描結果:", ""]
    for c in top5:
        sc = c.get("lana_score", "N/A")
        ch = c.get("change", 0)
        lines.append(f"💰 {c['coin']}: {sc} 分 | 漲幅 {ch:+.1f}%")
    lines.append(f"\n⏰ {ts}  |  AI分析: {len(candidates)} 顆候選  |  達標: {len(qualified)} 個  |  推送: {pushed} 個")
    send_tg("\n".join(lines), reply_markup={
        "inline_keyboard": [
            [
                {"text": "⏸ 暫停4小時", "callback_data": "pause:4"},
                {"text": "⏸ 暫停8小時", "callback_data": "pause:8"},
            ],
            [
                {"text": "⏸ 永久暫停", "callback_data": "pause:0"},
                {"text": "▶️ 恢復推送", "callback_data": "resume"},
            ],
            [
                {"text": "📊 查看狀態", "callback_data": "status"},
            ]
        ]
    })

    print(f"完成，推送 {pushed} 個")

except Exception as e:
    print(f"錯誤: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)
