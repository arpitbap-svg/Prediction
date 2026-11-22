import csv
import io
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

API_URL = "https://draw.ar-lottery01.com/WinGo/WinGo_30S/GetHistoryIssuePage.json"
HISTORY_FILE = "prediction_history.json"
STATS_FILE = "saved_pair_stats.json"
IST = timezone(timedelta(hours=5, minutes=30))

# Default 16,906 valid CSV records (fallback)
DEFAULT_PAIR_STATS = {
    "0-0":{"big":85,"small":102},"0-1":{"big":80,"small":94},"0-2":{"big":100,"small":84},"0-3":{"big":73,"small":85},"0-4":{"big":81,"small":80},
    "0-5":{"big":87,"small":88},"0-6":{"big":75,"small":91},"0-7":{"big":95,"small":77},"0-8":{"big":97,"small":89},"0-9":{"big":80,"small":84},
    "1-0":{"big":92,"small":101},"1-1":{"big":92,"small":86},"1-2":{"big":96,"small":98},"1-3":{"big":84,"small":82},"1-4":{"big":88,"small":74},
    "1-5":{"big":84,"small":81},"1-6":{"big":91,"small":89},"1-7":{"big":94,"small":80},"1-8":{"big":99,"small":73},"1-9":{"big":85,"small":91},
    "2-0":{"big":85,"small":78},"2-1":{"big":89,"small":80},"2-2":{"big":83,"small":84},"2-3":{"big":97,"small":80},"2-4":{"big":72,"small":79},
    "2-5":{"big":95,"small":86},"2-6":{"big":66,"small":103},"2-7":{"big":87,"small":99},"2-8":{"big":79,"small":78},"2-9":{"big":85,"small":85},
    "3-0":{"big":78,"small":83},"3-1":{"big":74,"small":72},"3-2":{"big":80,"small":87},"3-3":{"big":92,"small":61},"3-4":{"big":79,"small":74},
    "3-5":{"big":73,"small":93},"3-6":{"big":73,"small":108},"3-7":{"big":63,"small":97},"3-8":{"big":84,"small":102},"3-9":{"big":74,"small":75},
    "4-0":{"big":79,"small":90},"4-1":{"big":62,"small":83},"4-2":{"big":85,"small":75},"4-3":{"big":105,"small":82},"4-4":{"big":77,"small":73},
    "4-5":{"big":84,"small":80},"4-6":{"big":80,"small":79},"4-7":{"big":72,"small":84},"4-8":{"big":80,"small":93},"4-9":{"big":87,"small":93},
    "5-0":{"big":76,"small":81},"5-1":{"big":96,"small":97},"5-2":{"big":90,"small":70},"5-3":{"big":74,"small":77},"5-4":{"big":79,"small":83},
    "5-5":{"big":85,"small":75},"5-6":{"big":105,"small":82},"5-7":{"big":84,"small":82},"5-8":{"big":90,"small":91},"5-9":{"big":81,"small":94},
    "6-0":{"big":90,"small":85},"6-1":{"big":105,"small":98},"6-2":{"big":73,"small":75},"6-3":{"big":60,"small":82},"6-4":{"big":84,"small":103},
    "6-5":{"big":88,"small":84},"6-6":{"big":70,"small":82},"6-7":{"big":81,"small":87},"6-8":{"big":86,"small":79},"6-9":{"big":81,"small":71},
    "7-0":{"big":90,"small":65},"7-1":{"big":98,"small":102},"7-2":{"big":86,"small":96},"7-3":{"big":86,"small":72},"7-4":{"big":99,"small":81},
    "7-5":{"big":93,"small":69},"7-6":{"big":90,"small":65},"7-7":{"big":80,"small":78},"7-8":{"big":83,"small":85},"7-9":{"big":86,"small":80},
    "8-0":{"big":105,"small":97},"8-1":{"big":79,"small":91},"8-2":{"big":86,"small":79},"8-3":{"big":86,"small":79},"8-4":{"big":93,"small":79},
    "8-5":{"big":90,"small":90},"8-6":{"big":75,"small":75},"8-7":{"big":84,"small":101},"8-8":{"big":85,"small":89},"8-9":{"big":95,"small":77},
    "9-0":{"big":83,"small":82},"9-1":{"big":92,"small":90},"9-2":{"big":84,"small":79},"9-3":{"big":85,"small":80},"9-4":{"big":80,"small":85},
    "9-5":{"big":90,"small":78},"9-6":{"big":84,"small":81},"9-7":{"big":69,"small":90},"9-8":{"big":78,"small":95},"9-9":{"big":93,"small":89}
}

state_lock = threading.Lock()
worker_lock = threading.Lock()
worker_started = False

pair_stats = DEFAULT_PAIR_STATS.copy()
csv_info = {
    "filename": "Default Embedded CSV",
    "valid_records": 16906,
    "saved_at": "Built-in"
}

state = {
    "status": "Starting...",
    "last_updated": None,
    "latest_period": None,
    "next_period": None,
    "pair": None,
    "prediction": None,
    "big_votes": 0,
    "small_votes": 0,
    "total_matches": 0,
    "big_pct": 0.0,
    "small_pct": 0.0,
    "history": []
}


def load_saved_stats():
    global pair_stats, csv_info
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if "pair_stats" in saved and "csv_info" in saved:
                    pair_stats = saved["pair_stats"]
                    csv_info = saved["csv_info"]
        except Exception:
            pass


def save_stats_to_disk(new_stats, new_info):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump({"pair_stats": new_stats, "csv_info": new_info}, f)


def parse_csv_content(text):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file khali hai ya headers nahi mile.")

    headers = {h.strip().lower(): h for h in reader.fieldnames}
    num_col = headers.get("actual number") or headers.get("number")
    res_col = headers.get("actual result") or headers.get("result")

    if not num_col:
        raise ValueError("CSV mein 'Actual Number' ya 'Number' column hona zaroori hai.")

    nums = []
    results = []

    for row in reader:
        raw_num = (row.get(num_col) or "").strip()
        if not raw_num:
            continue
        try:
            n = int(float(raw_num))
            if not (0 <= n <= 9):
                continue
        except ValueError:
            continue

        if res_col and (row.get(res_col) or "").strip():
            r = row[res_col].strip().upper()
            if r not in ("BIG", "SMALL"):
                r = "BIG" if n >= 5 else "SMALL"
        else:
            r = "BIG" if n >= 5 else "SMALL"

        nums.append(n)
        results.append(r)

    if len(nums) < 3:
        raise ValueError("CSV mein kam se kam 3 valid records hone chahiye.")

    new_stats = {f"{d1}-{d2}": {"big": 0, "small": 0} for d1 in range(10) for d2 in range(10)}
    for i in range(len(nums) - 2):
        key = f"{nums[i]}-{nums[i+1]}"
        if results[i + 2] == "BIG":
            new_stats[key]["big"] += 1
        else:
            new_stats[key]["small"] += 1

    return new_stats, len(nums)


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f)
    except Exception:
        pass


def get_size(num):
    return "BIG" if int(num) >= 5 else "SMALL"


def process_api_data(draw_list):
    if not draw_list or len(draw_list) < 2:
        return

    latest_item = draw_list[0]
    prev_item = draw_list[1]

    latest_period = str(latest_item["issueNumber"])
    latest_num = int(latest_item["number"])
    prev_num = int(prev_item["number"])

    result_map = {str(item["issueNumber"]): int(item["number"]) for item in draw_list}
    next_period = str(int(latest_period) + 1)
    pair_key = f"{prev_num}-{latest_num}"
    now_str = datetime.now(IST).strftime("%I:%M:%S %p")

    with state_lock:
        votes = pair_stats.get(pair_key, {"big": 0, "small": 0})
        big_v = votes["big"]
        small_v = votes["small"]
        total = big_v + small_v
        big_pct = round((big_v / total) * 100, 1) if total > 0 else 50.0
        small_pct = round((small_v / total) * 100, 1) if total > 0 else 50.0
        pred = "BIG" if big_v >= small_v else "SMALL"

        changed = False
        for row in state["history"]:
            if row["status"] == "PENDING" and row["period"] in result_map:
                actual_num = result_map[row["period"]]
                actual_size = get_size(actual_num)
                row["actual_num"] = actual_num
                row["actual_size"] = actual_size
                row["status"] = "WIN" if row["prediction"] == actual_size else "LOSS"
                changed = True

        existing_periods = {row["period"] for row in state["history"]}
        if next_period not in existing_periods:
            state["history"].insert(0, {
                "period": next_period,
                "pair": f"{prev_num} → {latest_num}",
                "prediction": pred,
                "actual_num": None,
                "actual_size": None,
                "status": "PENDING",
                "time": now_str
            })
            changed = True

        if changed:
            save_history(state["history"])

        state["status"] = "Live"
        state["last_updated"] = now_str
        state["latest_period"] = latest_period
        state["next_period"] = next_period
        state["pair"] = [prev_num, latest_num]
        state["prediction"] = pred
        state["big_votes"] = big_v
        state["small_votes"] = small_v
        state["total_matches"] = total
        state["big_pct"] = big_pct
        state["small_pct"] = small_pct


def background_worker():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://draw.ar-lottery01.com/",
        "Origin": "https://draw.ar-lottery01.com"
    }
    while True:
        try:
            resp = requests.get(f"{API_URL}?ts={int(time.time() * 1000)}", headers=headers, timeout=10)
            if resp.status_code == 200:
                payload = resp.json()
                draw_list = payload.get("data", {}).get("list", [])
                if draw_list:
                    process_api_data(draw_list)
                else:
                    with state_lock:
                        state["status"] = "API Empty"
            else:
                with state_lock:
                    state["status"] = f"API {resp.status_code}"
        except Exception as e:
            with state_lock:
                state["status"] = f"Err: {type(e).__name__}"
        time.sleep(3)


def ensure_worker_started():
    global worker_started
    if not worker_started:
        with worker_lock:
            if not worker_started:
                load_saved_stats()
                state["history"] = load_history()
                t = threading.Thread(target=background_worker, daemon=True)
                t.start()
                worker_started = True


@app.before_request
def boot_worker():
    ensure_worker_started()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    with state_lock:
        wins = 0
        losses = 0
        cur_win = 0
        cur_loss = 0
        max_win_streak = 0
        max_loss_streak = 0

        for r in reversed(state["history"]):
            st = r["status"]
            if st == "WIN":
                wins += 1
                cur_win += 1
                cur_loss = 0
                if cur_win > max_win_streak:
                    max_win_streak = cur_win
            elif st == "LOSS":
                losses += 1
                cur_loss += 1
                cur_win = 0
                if cur_loss > max_loss_streak:
                    max_loss_streak = cur_loss

        total_settled = wins + losses
        win_rate = round((wins / total_settled) * 100, 1) if total_settled > 0 else 0.0

        return jsonify({
            **state,
            "csv_info": csv_info,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak
        })


@app.route("/api/upload-csv", methods=["POST"])
def upload_csv():
    global pair_stats, csv_info
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Koi CSV file select nahi ki gayi."}), 400

    file = request.files["file"]
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return jsonify({"ok": False, "error": "Sirf .csv file upload karein."}), 400

    try:
        content = file.read().decode("utf-8-sig", errors="ignore")
        new_stats, valid_count = parse_csv_content(content)
        new_info = {
            "filename": file.filename,
            "valid_records": valid_count,
            "saved_at": datetime.now(IST).strftime("%d %b, %I:%M %p")
        }

        with state_lock:
            pair_stats = new_stats
            csv_info = new_info
            save_stats_to_disk(new_stats, new_info)

        return jsonify({"ok": True, "csv_info": new_info})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/clear", methods=["POST"])
def api_clear():
    with state_lock:
        state["history"] = []
        save_history([])
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    ensure_worker_started()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
