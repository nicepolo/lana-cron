"""
trigger.py - LANA Cron v11
流程：
1. 處理 TG 指令（/pause /resume /status）
2. 檢查手動暫停開關 → 若暫停中則跳過本輪（省 API 費用）
3. /api/scan 取所有幣分數
4. 【新增】動能突破偵測：vol_ratio >= 2.0 且 lana_score 本輪比上輪高 ≥15 分 → 標記 ⚡ 優先推送
5. 分數 >= MIN_SCORE 的幣 → 呼叫 /api/ai_analyze 深度分析
6. AI 評分 < PUSH_MIN_AI_SCORE 的訊號直接排除
7. AI 說 LONG/SHORT 且評分達標的訊號依評分排序，只推最強的前 TOP_N_PUSH 個
8. AI 說 WATCH → 靜默跳過
9. 同一顆幣 4 小時冷卻（由 app.py 伺服器端記憶體處理）
"""

import requests, os, sys, time, hashlib, html, json
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCAN_URL     = os.getenv("SCAN_URL", "https://web-production-7cdf9.up.railway.app/api/scan")
AI_URL       = os.getenv("AI_URL", "https://web-production-7cdf9.up.railway.app/api/ai_analyze")
CTRL_URL     = os.getenv("CTRL_URL", "https://web-production-7cdf9.up.railway.app/api/push_control")
PAPER_MARK_URL = os.getenv("PAPER_MARK_URL", "https://web-production-7cdf9.up.railway.app/api/paper/mark")
POSITION_MONITOR_URL = os.getenv("POSITION_MONITOR_URL", "https://web-production-7cdf9.up.railway.app/api/positions/monitor")
BOT_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_SCORE         = int(os.getenv("MIN_SCORE", "60"))
TOP_N_PUSH        = int(os.getenv("TOP_N_PUSH", "3"))
PUSH_MIN_AI_SCORE = min(int(os.getenv("PUSH_MIN_AI_SCORE", "60")), 60)
SECONDARY_PUSH_MIN_AI_SCORE = min(int(os.getenv("SECONDARY_PUSH_MIN_AI_SCORE", "55")), 55)
MAX_AI_CANDIDATES  = min(int(os.getenv("MAX_AI_CANDIDATES", "4")), 4)
MIN_DIRECTION_SCORE = min(int(os.getenv("MIN_DIRECTION_SCORE", "65")), 65)
MAX_AI_ABS_CHANGE_24H = max(float(os.getenv("MAX_AI_ABS_CHANGE_24H", "45")), 45)
AI_REQUEST_DELAY_SEC = max(float(os.getenv("AI_REQUEST_DELAY_SEC", "3")), 3)
ALLOW_RULES_SIGNAL_PUSH = os.getenv("ALLOW_RULES_SIGNAL_PUSH", "true").strip().lower() in ("1", "true", "yes", "on")
FORCE_REANALYZE_BEFORE_PUSH = os.getenv("FORCE_REANALYZE_BEFORE_PUSH", "true").strip().lower() in ("1", "true", "yes", "on")
FORCE_REANALYZE_TOP_N = max(0, min(int(os.getenv("FORCE_REANALYZE_TOP_N", "1")), 1))
PUSH_BEST_SECONDARY_WHEN_EMPTY = os.getenv("PUSH_BEST_SECONDARY_WHEN_EMPTY", "true").strip().lower() in ("1", "true", "yes", "on")

# ── 動能突破參數 ──
MOMENTUM_VOL_RATIO_MIN  = float(os.getenv("MOMENTUM_VOL_RATIO_MIN", "2.0"))   # 量能倍數門檻
MOMENTUM_SCORE_JUMP_MIN = float(os.getenv("MOMENTUM_SCORE_JUMP_MIN", "15"))   # 分數跳升門檻
MOMENTUM_AI_SCORE_MIN   = int(os.getenv("MOMENTUM_AI_SCORE_MIN", "55"))       # 動能突破較寬鬆的AI門檻

# 上輪分數快取（/tmp 在同一 Railway 執行緒內可存活）
PREV_SCORE_CACHE = Path("/tmp/lana_prev_scores.json")

TZ_TAIPEI = timezone(timedelta(hours=8))

# ── 上輪分數讀寫 ──────────────────────────────────────────
def load_prev_scores() -> dict:
    try:
        if PREV_SCORE_CACHE.exists():
            return json.loads(PREV_SCORE_CACHE.read_text())
    except Exception:
        pass
    return {}

def save_prev_scores(scores: dict):
    try:
        PREV_SCORE_CACHE.write_text(json.dumps(scores))
    except Exception as e:
        print(f"  快取寫入失敗: {e}")

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
    try:
        r = requests.get(CTRL_URL, timeout=8)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"查詢推送狀態失敗: {e}，預設繼續推送")
    return {"should_push": True}

def mark_paper_positions():
    try:
        r = requests.post(PAPER_MARK_URL, timeout=30)
        if r.ok:
            summary = r.json().get("summary", {})
            print(f"  Paper Trading: 持倉 {summary.get('open_trades', 0)}，已實現 {summary.get('realized_pnl', 0)}")
        else:
            print(f"  Paper Trading 更新失敗 HTTP {r.status_code}")
    except Exception as e:
        print(f"  Paper Trading 更新失敗: {e}")

def monitor_manual_positions():
    try:
        response = requests.post(POSITION_MONITOR_URL, timeout=90)
        if not response.ok:
            print(f"  持倉助手更新失敗 HTTP {response.status_code}")
            return
        data = response.json()
        alerts = data.get("alerts", [])
        print(f"  持倉助手: 追蹤 {data.get('monitored', 0)} 筆，通知 {len(alerts)} 筆")
        action_labels = {
            "ADD": "➕ 考慮加倉",
            "REDUCE_50": "✂️ 減倉 50%",
            "REDUCE_30": "✂️ 再減倉 30%",
            "REDUCE_OR_CLOSE": "⚠️ 減倉或平倉",
            "CLOSE": "🛑 立即平倉",
            "HOLD": "🟢 續抱",
        }
        for alert in alerts:
            action = alert.get("action", "HOLD")
            message = (
                f"🧭 <b>{alert.get('coin')}/USDT 持倉更新</b>\n\n"
                f"{action_labels.get(action, action)}\n"
                f"{html.escape(str(alert.get('message', '')))}\n\n"
                f"目前：{alert.get('price')}（{alert.get('pnl_pct', 0):+.2f}% / "
                f"{alert.get('r_multiple', 0):+.2f}R）\n"
                f"價格來源：{alert.get('price_source') or alert.get('exchange') or '未知'}\n"
                f"動能止損：{alert.get('stop_loss')}\n"
                f"目標1：{alert.get('target_1')}｜目標2：{alert.get('target_2')}"
            )
            send_tg(message, reply_markup={"inline_keyboard": [[
                {"text": "📊 查看持倉", "callback_data": "positions_status"},
                {"text": "🏁 已平倉", "callback_data": f"position_close:{alert.get('position_id')}"},
            ]]})
    except Exception as e:
        print(f"  持倉助手更新失敗: {e}")

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

def ai_analyze(coin, price, change_24h, force=False):
    try:
        r = requests.post(
            AI_URL,
            json={
                "symbol": coin,
                "price": price,
                "change_24h": change_24h,
                "force": force,
            },
            timeout=45
        )
        if r.ok:
            data = r.json()
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

def format_signal(coin, ai_result, scan_score, change, price, scan_coin=None, reanalyzed_at=None, momentum_burst=False):
    score     = ai_result.get("score", scan_score)
    model     = str(ai_result.get("model") or "").lower()
    is_rules  = model == "rules"
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

    direction = ai_result.get("direction", "WATCH")
    direction_text = "🟢 模擬做多" if direction == "LONG" else "🔴 模擬做空" if direction == "SHORT" else "⚪ 觀望"
    score_label = "觀察分數" if is_rules else "AI分數"

    # 動能突破標題加 ⚡
    header = f"⚡ <b>{coin}/USDT (OKX) — 動能突破</b>" if momentum_burst else f"📡 <b>{coin}/USDT (OKX)</b>"

    lines = [
        header,
        f"現價: {price}  📈 24h {change:+.1f}%",
        f"方向: <b>{direction_text}</b>（Paper Trading）",
        f"{score_label}: {score}/100  信心: {conf_label}",
        f"規則分數: LONG {ai_result.get('long_score', 'N/A')} / SHORT {ai_result.get('short_score', 'N/A')}",
        f"RSI 1H: {rsi_str}  量能: {vr_str}  FR: {fr_str}",
    ]
    if momentum_burst:
        lines.append("⚡ 量能放大 + 分數急升，動能剛啟動，請留意入場時機")
    if is_rules:
        lines.append("⚠️ Gemini 目前限流或忙碌，這是規則模式，只能觀察/模擬，不建議下單。")
    if direction in ("LONG", "SHORT"):
        lines.append(
            f"模擬價位: 進場 {ai_result.get('entry_zone')} / 止損 {ai_result.get('stop_loss')} / "
            f"TP1 {ai_result.get('target_1')} / TP2 {ai_result.get('target_2')}"
        )
    if summary:
        lines.append(f"\n📌 {summary}")
    if reason:
        lines.append(f"<i>{reason}</i>")

    lines.append(f"\n⏰ {now_str}")
    if reanalyzed_at:
        lines.append(f"\n🔁 已自動重析：{reanalyzed_at}")
    return "\n".join(lines)


# ── 主流程 ──────────────────────────────────────────────────
try:
    now_str = datetime.now(TZ_TAIPEI).strftime("%H:%M")
    print(f"[{now_str}] 開始掃描...")

    handle_tg_commands()
    mark_paper_positions()
    monitor_manual_positions()

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

    # ── 讀取上輪分數快取，更新後儲存 ──
    prev_scores = load_prev_scores()
    curr_scores = {c["coin"]: c.get("lana_score", 0) for c in coins}
    save_prev_scores(curr_scores)

    # ── 動能突破判斷 ──
    def is_momentum_burst(c):
        coin = c["coin"]
        vr = float(c.get("vol_ratio") or 0)
        curr = float(c.get("lana_score") or 0)
        prev = float(prev_scores.get(coin) or 0)
        score_jump = curr - prev
        return (
            vr >= MOMENTUM_VOL_RATIO_MIN
            and score_jump >= MOMENTUM_SCORE_JUMP_MIN
            and c.get("rule_direction") in ("LONG", "SHORT")
            and abs(float(c.get("change") or 0)) <= MAX_AI_ABS_CHANGE_24H
        )

    def candidate_score(c):
        long_score = c.get("long_score") or 0
        short_score = c.get("short_score") or 0
        return max(c.get("lana_score") or 0, long_score, short_score)

    def is_ai_candidate(c):
        change = abs(float(c.get("change") or 0))
        if change > MAX_AI_ABS_CHANGE_24H:
            return False
        if c.get("rule_direction") not in ("LONG", "SHORT"):
            return False
        return (c.get("lana_score") or 0) >= MIN_SCORE or candidate_score(c) >= MIN_DIRECTION_SCORE

    # 動能突破幣優先，其餘按原邏輯
    burst_coins = [c for c in coins if is_momentum_burst(c)]
    burst_set   = {c["coin"] for c in burst_coins}
    if burst_coins:
        print(f"  ⚡ 動能突破偵測: {[c['coin'] for c in burst_coins]}")

    candidates = [c for c in coins if is_ai_candidate(c)]
    candidates.sort(key=candidate_score, reverse=True)
    # 動能突破的幣排到最前面
    candidates = sorted(candidates, key=lambda c: (0 if c["coin"] in burst_set else 1, -candidate_score(c)))
    candidates = candidates[:max(1, MAX_AI_CANDIDATES)]
    print(f"  候選 (LANA>={MIN_SCORE} 或方向分>={MIN_DIRECTION_SCORE}): {len(candidates)} 顆")

    recent_fps = tg_recent_fingerprints()

    qualified = []
    secondary_pool = []
    for idx, c in enumerate(candidates):
        coin   = c["coin"]
        score  = c.get("lana_score", 0)
        change = c.get("change", 0)
        price  = c.get("price", 0)
        burst  = coin in burst_set

        print(f"  AI 分析 {coin} (scan:{score}分{'  ⚡動能突破' if burst else ''})...")
        force_ai = FORCE_REANALYZE_BEFORE_PUSH and idx < FORCE_REANALYZE_TOP_N
        ai_result = ai_analyze(coin, price, change, force=force_ai)
        reanalyzed_at = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M") if force_ai else None
        time.sleep(AI_REQUEST_DELAY_SEC)

        if not ai_result:
            print(f"  {coin} AI 分析失敗，跳過")
            continue

        direction = ai_result.get("direction", "WATCH")
        ai_score  = ai_result.get("score", 0)
        ai_model  = ai_result.get("model", "?")
        print(f"  {coin} AI結果: {direction} {ai_score}分 [{ai_model}]")

        if str(ai_model).lower() == "rules" and not ALLOW_RULES_SIGNAL_PUSH:
            print(f"  {coin} 使用規則備援模式，跳過推送")
            continue

        if direction not in ("LONG", "SHORT"):
            print(f"  {coin} AI說{direction}，跳過")
            continue

        # 動能突破幣用較寬鬆的 AI 門檻
        # v5: 高分（>85）且非動能突破 → AI 門檻提高到 75，防止高分陷阱
        momentum_dominated = ai_result.get("momentum_dominated", False)
        if not burst and score > 85 and momentum_dominated:
            effective_min = 75
            print(f"  {coin} 高分({score})且漲幅主導，AI門檻提高至75")
        elif not burst and score > 85:
            effective_min = max(PUSH_MIN_AI_SCORE, 75)
            print(f"  {coin} 高分({score})，AI門檻提高至75")
        else:
            effective_min = MOMENTUM_AI_SCORE_MIN if burst else PUSH_MIN_AI_SCORE

        if ai_score < effective_min:
            if PUSH_BEST_SECONDARY_WHEN_EMPTY and ai_score >= SECONDARY_PUSH_MIN_AI_SCORE:
                print(f"  {coin} AI評分{ai_score}低於門檻{effective_min}，加入次級候選")
                secondary_pool.append({
                    "coin": coin, "ai_result": ai_result, "ai_score": ai_score,
                    "score": score, "change": change, "price": price, "scan_coin": c,
                    "secondary": True, "reanalyzed_at": reanalyzed_at, "burst": burst
                })
                continue
            print(f"  {coin} AI評分{ai_score}低於門檻{effective_min}，跳過")
            continue

        qualified.append({
            "coin": coin, "ai_result": ai_result, "ai_score": ai_score,
            "score": score, "change": change, "price": price, "scan_coin": c,
            "secondary": False, "reanalyzed_at": reanalyzed_at, "burst": burst
        })

    # 動能突破排最前，其次依AI分排序
    qualified.sort(key=lambda x: (0 if x["burst"] else 1, -x["ai_score"]))
    if qualified:
        to_send = qualified[:TOP_N_PUSH]
        skipped_n = len(qualified) - len(to_send)
    elif PUSH_BEST_SECONDARY_WHEN_EMPTY and secondary_pool:
        secondary_pool.sort(key=lambda x: (0 if x["burst"] else 1, -x["ai_score"]))
        to_send = secondary_pool[:1]
        skipped_n = 0
        print(f"  本輪無強訊號，改推最佳次級觀察訊號：{to_send[0]['coin']} {to_send[0]['ai_score']}分")
    else:
        to_send = []
        skipped_n = 0

    pushed = 0
    for q in to_send:
        coin = q["coin"]
        msg = format_signal(
            coin, q["ai_result"], q["score"], q["change"], q["price"],
            scan_coin=q["scan_coin"], reanalyzed_at=q.get("reanalyzed_at"),
            momentum_burst=q.get("burst", False)
        )
        if str(q["ai_result"].get("model", "")).lower() == "rules":
            msg = "⚠️ <b>規則模式觀察訊號</b>\nGemini 目前限流或忙碌，這不是 Gemini 深度分析；若要做，請輕倉並嚴守止損。\n\n" + msg
        elif q.get("secondary"):
            msg = "⚠️ <b>輕倉觀察訊號</b>\n本輪沒有強訊號，這是最佳次級訊號；若要做，請縮小倉位並嚴守止損。\n\n" + msg

        fp = hashlib.md5(msg[:80].encode()).hexdigest()[:8]
        if fp in recent_fps:
            print(f"  {coin} 10分鐘內已推送，跳過")
            continue

        keyboard = []
        signal_id = q["ai_result"].get("signal_id")
        if signal_id and str(q["ai_result"].get("model", "")).lower() != "rules":
            keyboard.append([
                {"text": "✅ 已下單，開始追蹤", "callback_data": f"entered:{signal_id}"}
            ])
        keyboard.append([
            {"text": "🔄 重新分析", "callback_data": f"reanalyze:{coin}"},
            {"text": "⏸ 暫停4小時", "callback_data": "pause:4"}
        ])
        ok = send_tg(msg, reply_markup={"inline_keyboard": keyboard})
        if ok:
            recent_fps.add(fp)
            pushed += 1
            print(f"  ✅ {coin} 推送完成")
        else:
            print(f"  ❌ {coin} 推送失敗")
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
        burst_tag = " ⚡" if c["coin"] in burst_set else ""
        lines.append(f"💰 {c['coin']}: {sc} 分 | 漲幅 {ch:+.1f}%{burst_tag}")
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
