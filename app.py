import os, json, time, threading, requests
from flask import Flask, send_file, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=BASE_DIR)
STATE_FILE = os.path.join(BASE_DIR, 'state.json')
LOCK = threading.Lock()

API_URL = "https://draw.ar-lottery01.com/WinGo/WinGo_30S/GetHistoryIssuePage.json"
PROXIES = [
    lambda u: u,
    lambda u: f"https://api.allorigins.win/raw?url={requests.utils.quote(u)}",
    lambda u: f"https://corsproxy.io/?{requests.utils.quote(u)}"
]

def default_state():
    return {
        "recs": {"30s": [], "1m": []},
        "lastSeen": {"30s": None, "1m": None},
        "pred": {
            "level": 0, "lastPred": None, "lastNum": None, "lastPeriod": None,
            "lastMode": "30s", "lossCount": 0, "winStreak": 0, "bestStreak": 0,
            "totalWins": 0, "totalLoss": 0, "step": 1, "lastEngines": None, "jackpots": 0
        },
        "history": [],
        "updatedAt": 0
    }

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return default_state()

def save_state(st):
    st["updatedAt"] = int(time.time())
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(st, f)
    except Exception:
        pass

STATE = load_state()

def fetch_live_wingo():
    u = f"{API_URL}?_t={int(time.time()*1000)}"
    for px in PROXIES:
        try:
            r = requests.get(px(u), timeout=6)
            if r.ok:
                d = r.json()
                lst = d.get("data", {}).get("list") or d.get("list") or []
                out = []
                for it in lst:
                    n = int(it.get("number", it.get("openCode", -1)))
                    p = str(it.get("issueNumber", it.get("period", "")))
                    if 0 <= n <= 9 and p:
                        out.append({"period": p, "number": n})
                if out:
                    return out[:20]
        except Exception:
            continue
    return []

def next_period(p):
    try:
        return str(int(p) + 1)
    except Exception:
        return p

def server_predict(recs, target_period, loss_count):
    nums = [r["number"] for r in recs[:30]]
    if len(nums) < 5:
        return "BIG", [6, 8], 0.65

    big_votes, small_votes = 0.0, 0.0
    last = nums[0]
    last_bs = "BIG" if last >= 5 else "SMALL"

    streak = 1
    for n in nums[1:]:
        if ("BIG" if n >= 5 else "SMALL") == last_bs:
            streak += 1
        else:
            break
    if streak >= 3:
        if last_bs == "BIG": small_votes += 2.2
        else: big_votes += 2.2
    else:
        if last_bs == "BIG": big_votes += 1.2
        else: small_votes += 1.2

    b20 = sum(1 for n in nums[:20] if n >= 5)
    if b20 >= 13: small_votes += 1.8
    elif b20 <= 7: big_votes += 1.8
    elif b20 >= 10: big_votes += 1.0
    else: small_votes += 1.0

    alt = sum(1 for i in range(min(5, len(nums)-1)) if (nums[i]>=5) != (nums[i+1]>=5))
    if alt >= 4:
        if last_bs == "BIG": small_votes += 1.6
        else: big_votes += 1.6

    avg5 = sum(nums[:5]) / 5.0
    avg10 = sum(nums[:10]) / min(10, len(nums))
    if avg5 > avg10: big_votes += 1.3
    else: small_votes += 1.3

    seed = int(str(target_period)[-3:] or "0")
    ent = (seed ^ sum(nums[:5])) % 10
    if ent >= 5: big_votes += 1.0
    else: small_votes += 1.0

    call = "BIG" if big_votes >= small_votes else "SMALL"
    tot = big_votes + small_votes
    conf = round(min(95, max(55, (max(big_votes, small_votes) / tot) * 100))) / 100.0

    pool = [5, 6, 7, 8, 9] if call == "BIG" else [0, 1, 2, 3, 4]
    opp = [0, 1, 2, 3, 4] if call == "BIG" else [5, 6, 7, 8, 9]
    main_num = sorted(pool, key=lambda x: nums[:10].count(x))[0]
    back_num = sorted(opp, key=lambda x: -nums[:10].count(x))[0]
    return call, [main_num, back_num], conf

def background_worker():
    while True:
        try:
            incoming = fetch_live_wingo()
            if incoming:
                with LOCK:
                    existing = {r["period"]: r for r in STATE["recs"]["30s"]}
                    for r in incoming:
                        existing[r["period"]] = r
                    merged = sorted(existing.values(), key=lambda x: x["period"], reverse=True)[:1200]
                    STATE["recs"]["30s"] = merged

                    latest = merged[0]
                    if latest["period"] != STATE["lastSeen"]["30s"]:
                        STATE["lastSeen"]["30s"] = latest["period"]
                        pred = STATE["pred"]
                        actual_num = latest["number"]
                        actual_type = "BIG" if actual_num >= 5 else "SMALL"

                        if pred["lastPeriod"] == latest["period"] and pred["lastPred"] and pred["lastPred"] != "SKIP":
                            side_win = (pred["lastPred"] == actual_type)
                            jp = actual_num in (pred["lastNum"] or [])
                            win = side_win or jp
                            if win:
                                pred["lossCount"] = 0
                                pred["winStreak"] += 1
                                pred["totalWins"] += 1
                                if jp: pred["jackpots"] = pred.get("jackpots", 0) + 1
                                if pred["winStreak"] > pred["bestStreak"]:
                                    pred["bestStreak"] = pred["winStreak"]
                            else:
                                pred["lossCount"] += 1
                                pred["winStreak"] = 0
                                pred["totalLoss"] += 1

                            STATE["history"].insert(0, {
                                "period": latest["period"],
                                "mode": "30s",
                                "prediction": pred["lastPred"],
                                "predNum": pred["lastNum"],
                                "actual": actual_type,
                                "actualNum": actual_num,
                                "win": win,
                                "jackpot": jp,
                                "sideWin": side_win,
                                "skip": False,
                                "step": pred["step"],
                                "streak": pred["lossCount"],
                                "conf": pred.get("lastConf", 0.75)
                            })
                            STATE["history"] = STATE["history"][:100]

                        target = next_period(latest["period"])
                        call, nums, conf = server_predict(merged, target, pred["lossCount"])
                        pred["step"] = pred["lossCount"] + 1
                        pred["lastPred"] = call
                        pred["lastNum"] = nums
                        pred["lastPeriod"] = target
                        pred["lastMode"] = "30s"
                        pred["lastConf"] = conf
                        save_state(STATE)
        except Exception as e:
            print("Worker Error:", e)
        time.sleep(2)

t = threading.Thread(target=background_worker, daemon=True)
t.start()

@app.route('/')
def home():
    html_path = os.path.join(BASE_DIR, 'index.html')
    if not os.path.exists(html_path):
        html_path = os.path.join(BASE_DIR, 'templates', 'index.html')
    if not os.path.exists(html_path):
        html_path = os.path.join(BASE_DIR, 'Wingo_Master_AllLogic_Voting.html')
    return send_file(html_path)

@app.route('/api/state', methods=['GET', 'POST'])
def sync_state():
    global STATE
    with LOCK:
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            if "pred" in data and data["pred"].get("lastPeriod") == STATE["pred"].get("lastPeriod"):
                STATE["pred"].update(data["pred"])
                save_state(STATE)
        return jsonify(STATE)

@app.route('/api/reset', methods=['POST'])
def reset_state():
    global STATE
    with LOCK:
        STATE = default_state()
        save_state(STATE)
    return jsonify({"ok": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
