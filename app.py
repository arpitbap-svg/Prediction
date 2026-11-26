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
SETTINGS_FILE = "app_settings.json"
IST = timezone(timedelta(hours=5, minutes=30))

MAX_STRONG_LOSS = 4      # Strong Time tab ke liye max 4 loss
MAX_EXACT_SPOT_LOSS = 2  # Exact Spots tab ke liye max 2 loss

state_lock = threading.Lock()
worker_lock = threading.Lock()
worker_started = False

# Koi default CSV data nahi hai — upload karne ke baad hi load hoga
pair_stats = {}
csv_loaded = False
csv_info = {
    "filename": None,
    "valid_records": 0,
    "saved_at": None
}

settings = {
    "use_live_history": True
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
    "live_matches": 0,
    "big_pct": 0.0,
    "small_pct": 0.0,
    "history": []
}


def load_settings():
    global settings
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "use_live_history" in data:
                    settings["use_live_history"] = bool(data["use_live_history"])
        except Exception:
            pass


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except Exception:
        pass


def load_saved_stats():
    global pair_stats, csv_info, csv_loaded
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if saved.get("pair_stats") and saved.get("csv_info", {}).get("filename"):
                    pair_stats = saved["pair_stats"]
                    csv_info = saved["csv_info"]
                    csv_loaded = True
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


def format_hour_slot(h):
    def fmt(hr):
        hr = hr % 24
        ampm = "AM" if hr < 12 else "PM"
        h12 = hr % 12
        if h12 == 0:
            h12 = 12
        return f"{h12:02d} {ampm}"
    return f"{fmt(h)} – {fmt(h + 1)}"


def extract_row_time_info(row):
    ts = row.get("ts")
    if ts:
        try:
            dt = datetime.fromtimestamp(int(ts), tz=IST)
            return (
                dt.hour,
                dt.minute,
                dt.strftime("%I:%M %p"),
                dt.strftime("%Y-%m-%d"),
                dt.strftime("%d %b"),
                dt.strftime("%A"),
                int(ts)
            )
        except Exception:
            pass

    t_str = row.get("time")
    if t_str:
        try:
            dt = datetime.strptime(t_str, "%I:%M:%S %p")
            return dt.hour, dt.minute, dt.strftime("%I:%M %p"), "Saved", "Saved", None, None
        except Exception:
            pass
    return None, None, None, None, None, None, None


def get_live_pair_votes(history, pair_key):
    live_big = 0
    live_small = 0
    target_pair = pair_key.replace("-", " → ")
    for row in history:
        if row.get("pair") == target_pair and row.get("actual_size") in ("BIG", "SMALL"):
            if row["actual_size"] == "BIG":
                live_big += 1
            else:
                live_small += 1
    return live_big, live_small


def compute_good_time_notes(history):
    settled = [r for r in reversed(history) if r.get("status") in ("WIN", "LOSS")]
    notes = []
    seg = None

    def finalize_segment(s, is_active=False):
        if not s:
            return
        total = s["wins"] + s["losses"]
        if s["wins"] >= 3 and total >= 5 and s["max_loss"] <= MAX_STRONG_LOSS:
            win_rate = round((s["wins"] / total) * 100, 1)
            if s["start_ts"] and s["end_ts"]:
                dur_mins = max(1, round((s["end_ts"] - s["start_ts"]) / 60))
            else:
                dur_mins = max(1, round((total * 30) / 60))

            notes.append({
                "date_label": s["date_label"],
                "day": s["day"] or "",
                "start_time": s["start_time"],
                "end_time": "Now" if is_active else s["end_time"],
                "duration_min": dur_mins,
                "wins": s["wins"],
                "losses": s["losses"],
                "total": total,
                "win_rate": win_rate,
                "max_loss": s["max_loss"],
                "active": is_active
            })

    consec_losses = 0
    pending_losses = 0

    for row in settled:
        st = row["status"]
        t_str = row.get("time", "--:--")
        _, _, _, _, date_label, weekday, ts = extract_row_time_info(row)

        if seg is None:
            if st == "WIN":
                seg = {
                    "date_label": date_label or "Today",
                    "day": weekday[:3] if weekday else "",
                    "start_time": t_str,
                    "end_time": t_str,
                    "start_ts": ts,
                    "end_ts": ts,
                    "wins": 1,
                    "losses": 0,
                    "max_loss": 0
                }
                consec_losses = 0
                pending_losses = 0
            else:
                consec_losses += 1
                if consec_losses > MAX_STRONG_LOSS:
                    consec_losses = 0
        else:
            if st == "WIN":
                seg["wins"] += 1
                seg["losses"] += pending_losses
                seg["end_time"] = t_str
                seg["end_ts"] = ts
                if consec_losses > seg["max_loss"]:
                    seg["max_loss"] = consec_losses
                consec_losses = 0
                pending_losses = 0
            else:
                consec_losses += 1
                pending_losses += 1
                if consec_losses > MAX_STRONG_LOSS:
                    finalize_segment(seg, is_active=False)
                    seg = None
                    consec_losses = 0
                    pending_losses = 0

    if seg is not None:
        finalize_segment(seg, is_active=True)

    notes.reverse()
    return notes


def compute_strong_times(history):
    settled = [r for r in reversed(history) if r.get("status") in ("WIN", "LOSS")]

    hour_buckets = {}
    day_buckets = {}
    daily_hour_buckets = {}

    for row in settled:
        st = row["status"]
        hour, _, _, date_key, date_label, weekday, ts = extract_row_time_info(row)

        if hour is not None:
            hb = hour_buckets.setdefault(hour, {
                "wins": 0, "losses": 0, "cur_loss": 0, "max_loss": 0, "by_date": {}
            })
            if st == "WIN":
                hb["wins"] += 1
                hb["cur_loss"] = 0
            else:
                hb["losses"] += 1
                hb["cur_loss"] += 1
                if hb["cur_loss"] > hb["max_loss"]:
                    hb["max_loss"] = hb["cur_loss"]

            if date_key:
                db = hb["by_date"].setdefault(date_key, {
                    "wins": 0, "losses": 0, "cur_loss": 0, "max_loss": 0, "min_ts": ts, "max_ts": ts
                })
                if st == "WIN":
                    db["wins"] += 1
                    db["cur_loss"] = 0
                else:
                    db["losses"] += 1
                    db["cur_loss"] += 1
                    if db["cur_loss"] > db["max_loss"]:
                        db["max_loss"] = db["cur_loss"]
                if ts:
                    if db["min_ts"] is None or ts < db["min_ts"]:
                        db["min_ts"] = ts
                    if db["max_ts"] is None or ts > db["max_ts"]:
                        db["max_ts"] = ts

                dh_key = f"{date_key}_{hour:02d}"
                dh = daily_hour_buckets.setdefault(dh_key, {
                    "date_key": date_key, "date_label": date_label or date_key,
                    "day": weekday[:3] if weekday else "", "hour": hour,
                    "slot": format_hour_slot(hour), "wins": 0, "losses": 0,
                    "cur_loss": 0, "max_loss": 0, "min_ts": ts, "max_ts": ts
                })
                if st == "WIN":
                    dh["wins"] += 1
                    dh["cur_loss"] = 0
                else:
                    dh["losses"] += 1
                    dh["cur_loss"] += 1
                    if dh["cur_loss"] > dh["max_loss"]:
                        dh["max_loss"] = dh["cur_loss"]
                if ts:
                    if dh["min_ts"] is None or ts < dh["min_ts"]:
                        dh["min_ts"] = ts
                    if dh["max_ts"] is None or ts > dh["max_ts"]:
                        dh["max_ts"] = ts

        if weekday:
            wb = day_buckets.setdefault(weekday, {
                "wins": 0, "losses": 0, "cur_loss": 0, "max_loss": 0, "dates": set()
            })
            if st == "WIN":
                wb["wins"] += 1
                wb["cur_loss"] = 0
            else:
                wb["losses"] += 1
                wb["cur_loss"] += 1
                if wb["cur_loss"] > wb["max_loss"]:
                    wb["max_loss"] = wb["cur_loss"]
            if date_key:
                wb["dates"].add(date_key)

    hourly_list = []
    for h, hb in hour_buckets.items():
        total = hb["wins"] + hb["losses"]
        win_rate = round((hb["wins"] / total) * 100, 1) if total > 0 else 0.0
        days_tracked = len(hb["by_date"])
        strong_days = 0
        total_dur = 0
        for d_stat in hb["by_date"].values():
            d_tot = d_stat["wins"] + d_stat["losses"]
            d_wr = (d_stat["wins"] / d_tot) * 100 if d_tot > 0 else 0
            if d_tot >= 4 and d_stat["max_loss"] <= MAX_STRONG_LOSS and d_wr >= 50:
                strong_days += 1
            if d_stat["min_ts"] and d_stat["max_ts"]:
                total_dur += max(1, round((d_stat["max_ts"] - d_stat["min_ts"]) / 60))
            else:
                total_dur += max(1, round((d_tot * 30) / 60))

        avg_dur = round(total_dur / days_tracked) if days_tracked > 0 else 0
        is_strong = total >= 5 and hb["max_loss"] <= MAX_STRONG_LOSS and win_rate >= 50.0

        hourly_list.append({
            "hour": h,
            "slot": format_hour_slot(h),
            "wins": hb["wins"],
            "losses": hb["losses"],
            "total": total,
            "win_rate": win_rate,
            "max_loss_streak": hb["max_loss"],
            "days_tracked": days_tracked,
            "strong_days": strong_days,
            "avg_duration_min": avg_dur,
            "is_strong": is_strong
        })

    hourly_list.sort(key=lambda x: (x["is_strong"], x["strong_days"], -x["max_loss_streak"], x["win_rate"], x["total"]), reverse=True)

    daily_hours_list = []
    for dh in daily_hour_buckets.values():
        total = dh["wins"] + dh["losses"]
        win_rate = round((dh["wins"] / total) * 100, 1) if total > 0 else 0.0
        if dh["min_ts"] and dh["max_ts"]:
            dur_min = max(1, round((dh["max_ts"] - dh["min_ts"]) / 60))
        else:
            dur_min = max(1, round((total * 30) / 60))
        is_strong = total >= 4 and dh["max_loss"] <= MAX_STRONG_LOSS and win_rate >= 50.0

        daily_hours_list.append({
            "date_key": dh["date_key"],
            "date_label": dh["date_label"],
            "day": dh["day"],
            "hour": dh["hour"],
            "slot": dh["slot"],
            "wins": dh["wins"],
            "losses": dh["losses"],
            "total": total,
            "win_rate": win_rate,
            "max_loss_streak": dh["max_loss"],
            "duration_min": dur_min,
            "is_strong": is_strong
        })

    daily_hours_list.sort(key=lambda x: (x["date_key"], x["hour"]), reverse=True)

    day_list = []
    for day_name, wb in day_buckets.items():
        total = wb["wins"] + wb["losses"]
        win_rate = round((wb["wins"] / total) * 100, 1) if total > 0 else 0.0
        is_strong = total >= 5 and wb["max_loss"] <= MAX_STRONG_LOSS and win_rate >= 50.0
        day_list.append({
            "day": day_name,
            "wins": wb["wins"],
            "losses": wb["losses"],
            "total": total,
            "win_rate": win_rate,
            "max_loss_streak": wb["max_loss"],
            "dates_count": len(wb["dates"]),
            "is_strong": is_strong
        })

    day_list.sort(key=lambda x: (x["is_strong"], -x["max_loss_streak"], x["win_rate"], x["total"]), reverse=True)

    strong_hours_only = [h for h in hourly_list if h["is_strong"]]
    strong_days_only = [d for d in day_list if d["is_strong"]]

    return {
        "best_hour": strong_hours_only[0] if strong_hours_only else (hourly_list[0] if hourly_list else None),
        "best_day": strong_days_only[0] if strong_days_only else (day_list[0] if day_list else None),
        "hours": hourly_list,
        "daily_hours": daily_hours_list,
        "days": day_list
    }


def compute_exact_spots(history):
    settled = [r for r in reversed(history) if r.get("status") in ("WIN", "LOSS")]

    clock_buckets = {}
    minute_buckets = {}
    period_buckets = {}

    def update_bucket(store, key, st):
        b = store.setdefault(key, {"wins": 0, "losses": 0, "cur_loss": 0, "max_loss": 0})
        if st == "WIN":
            b["wins"] += 1
            b["cur_loss"] = 0
        else:
            b["losses"] += 1
            b["cur_loss"] += 1
            if b["cur_loss"] > b["max_loss"]:
                b["max_loss"] = b["cur_loss"]

    for row in settled:
        st = row["status"]
        _, minute, clock_hm, _, _, _, _ = extract_row_time_info(row)

        if clock_hm:
            update_bucket(clock_buckets, clock_hm, st)
        if minute is not None:
            update_bucket(minute_buckets, f":{minute:02d} min", st)

        period_str = str(row.get("period") or "")
        if len(period_str) >= 4:
            p_code = f"#{period_str[-4:]}"
            update_bucket(period_buckets, p_code, st)

    def format_buckets(store, min_total=2):
        items = []
        for label, b in store.items():
            total = b["wins"] + b["losses"]
            if total < min_total:
                continue
            win_rate = round((b["wins"] / total) * 100, 1)
            if b["max_loss"] <= MAX_EXACT_SPOT_LOSS and win_rate >= 60.0:
                items.append({
                    "label": label,
                    "wins": b["wins"],
                    "losses": b["losses"],
                    "total": total,
                    "win_rate": win_rate,
                    "max_loss": b["max_loss"],
                    "back_to_back": b["losses"] == 0 and b["wins"] >= 2
                })
        items.sort(key=lambda x: (x["back_to_back"], -x["max_loss"], x["win_rate"], x["wins"]), reverse=True)
        return items[:25]

    return {
        "exact_clocks": format_buckets(clock_buckets, min_total=2),
        "exact_minutes": format_buckets(minute_buckets, min_total=3),
        "exact_periods": format_buckets(period_buckets, min_total=2)
    }


def calculate_votes_for_pair(pair_key, history):
    # Jab tak CSV upload nahi hoti, koi prediction nahi deni hai
    if not csv_loaded:
        return 0, 0, 0, 0, 0.0, 0.0, None

    csv_votes = pair_stats.get(pair_key, {"big": 0, "small": 0})
    big_v = csv_votes["big"]
    small_v = csv_votes["small"]
    live_total = 0

    if settings.get("use_live_history", True):
        live_big, live_small = get_live_pair_votes(history, pair_key)
        big_v += live_big
        small_v += live_small
        live_total = live_big + live_small

    total = big_v + small_v
    if total == 0:
        return 0, 0, 0, 0, 0.0, 0.0, None

    big_pct = round((big_v / total) * 100, 1)
    small_pct = round((small_v / total) * 100, 1)
    pred = "BIG" if big_v >= small_v else "SMALL"
    return big_v, small_v, total, live_total, big_pct, small_pct, pred


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
    now_dt = datetime.now(IST)
    now_str = now_dt.strftime("%I:%M:%S %p")
    now_ts = int(now_dt.timestamp())

    with state_lock:
        changed = False
        for row in state["history"]:
            if row["status"] == "PENDING" and row["period"] in result_map:
                actual_num = result_map[row["period"]]
                actual_size = get_size(actual_num)
                row["actual_num"] = actual_num
                row["actual_size"] = actual_size
                row["status"] = "WIN" if row["prediction"] == actual_size else "LOSS"
                changed = True

        big_v, small_v, total, live_total, big_pct, small_pct, pred = calculate_votes_for_pair(
            pair_key, state["history"]
        )

        # Sirf CSV uploaded hone aur valid prediction milne par hi history mein save karein
        if csv_loaded and pred is not None:
            existing_periods = {row["period"] for row in state["history"]}
            if next_period not in existing_periods:
                state["history"].insert(0, {
                    "period": next_period,
                    "pair": f"{prev_num} → {latest_num}",
                    "prediction": pred,
                    "actual_num": None,
                    "actual_size": None,
                    "status": "PENDING",
                    "time": now_str,
                    "ts": now_ts
                })
                changed = True

        if changed:
            save_history(state["history"])

        state["status"] = "Live" if csv_loaded else "Upload CSV"
        state["last_updated"] = now_str
        state["latest_period"] = latest_period
        state["next_period"] = next_period
        state["pair"] = [prev_num, latest_num]
        state["prediction"] = pred
        state["big_votes"] = big_v
        state["small_votes"] = small_v
        state["total_matches"] = total
        state["live_matches"] = live_total
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
                load_settings()
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
        good_notes = compute_good_time_notes(state["history"])
        strong_times = compute_strong_times(state["history"])
        exact_spots = compute_exact_spots(state["history"])

        return jsonify({
            **state,
            "csv_loaded": csv_loaded,
            "csv_info": csv_info,
            "use_live_history": settings["use_live_history"],
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "notes": good_notes,
            "strong_times": strong_times,
            "exact_spots": exact_spots
        })


@app.route("/api/toggle-live-history", methods=["POST"])
def toggle_live_history():
    data = request.get_json(silent=True) or {}
    new_val = bool(data.get("enabled", not settings["use_live_history"]))

    with state_lock:
        settings["use_live_history"] = new_val
        save_settings()

        if csv_loaded and state["pair"]:
            pair_key = f"{state['pair'][0]}-{state['pair'][1]}"
            big_v, small_v, total, live_total, big_pct, small_pct, pred = calculate_votes_for_pair(
                pair_key, state["history"]
            )
            state["prediction"] = pred
            state["big_votes"] = big_v
            state["small_votes"] = small_v
            state["total_matches"] = total
            state["live_matches"] = live_total
            state["big_pct"] = big_pct
            state["small_pct"] = small_pct

    return jsonify({"ok": True, "use_live_history": new_val})


@app.route("/api/upload-csv", methods=["POST"])
def upload_csv():
    global pair_stats, csv_info, csv_loaded
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Koi CSV file select nahi ki gayi."}), 400

    file = request.files["file"]
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return jsonify({"ok": False, "error": "Sirf .csv file upload karein."}), 400

    try:
        content = file.read().decode("utf-8-sig", errors="ignore")
        new_stats, valid_count = parse_csv_content(content)
        now_dt = datetime.now(IST)
        new_info = {
            "filename": file.filename,
            "valid_records": valid_count,
            "saved_at": now_dt.strftime("%d %b, %I:%M %p")
        }

        with state_lock:
            pair_stats = new_stats
            csv_info = new_info
            csv_loaded = True
            save_stats_to_disk(new_stats, new_info)

            # CSV upload hote hi current pair par turant prediction calculate karein
            if state["pair"]:
                pair_key = f"{state['pair'][0]}-{state['pair'][1]}"
                big_v, small_v, total, live_total, big_pct, small_pct, pred = calculate_votes_for_pair(
                    pair_key, state["history"]
                )
                state["status"] = "Live"
                state["prediction"] = pred
                state["big_votes"] = big_v
                state["small_votes"] = small_v
                state["total_matches"] = total
                state["live_matches"] = live_total
                state["big_pct"] = big_pct
                state["small_pct"] = small_pct

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
