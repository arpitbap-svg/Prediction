import os, json, time, threading, logging, csv, io
from datetime import datetime, timezone
from collections import Counter
import requests
from flask import Flask, jsonify, send_from_directory, Response, request
from sqlalchemy import create_engine, text, inspect

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
app = Flask(__name__, static_folder='static')
API_BASE = 'https://draw.ar-lottery01.com/WinGo/WinGo_30S/GetHistoryIssuePage.json?pageSize=10&pageNo={page}'
POLL_SECONDS = max(3, int(os.getenv('POLL_SECONDS', '3')))
API_CONNECT_TIMEOUT = max(2, float(os.getenv('API_CONNECT_TIMEOUT', '4')))
API_READ_TIMEOUT = max(3, float(os.getenv('API_READ_TIMEOUT', '7')))
API_RETRIES = max(1, min(5, int(os.getenv('API_RETRIES', '3'))))
collector_status = {'last_success': None, 'last_attempt': None, 'last_error': None, 'consecutive_failures': 0, 'last_rows': 0}
collector_status_lock = threading.Lock()
cycle_lock = threading.Lock()
DB_URL = os.getenv('DATABASE_URL', '').strip()
if DB_URL.startswith('postgres://'):
    DB_URL = DB_URL.replace('postgres://', 'postgresql+psycopg2://', 1)
elif DB_URL.startswith('postgresql://'):
    DB_URL = DB_URL.replace('postgresql://', 'postgresql+psycopg2://', 1)
if not DB_URL:
    DB_URL = 'sqlite:///wingo_local.db'
engine = create_engine(DB_URL, pool_pre_ping=True, future=True)

with open(os.path.join(os.path.dirname(__file__), 'methods.json'), encoding='utf-8') as f:
    METHODS = json.load(f)


def init_db():
    with engine.begin() as c:
        c.execute(text('''CREATE TABLE IF NOT EXISTS results (
            issue VARCHAR(80) PRIMARY KEY, number INTEGER NOT NULL, size VARCHAR(10) NOT NULL,
            colour VARCHAR(10) NOT NULL, draw_time VARCHAR(80), saved_at VARCHAR(40) NOT NULL
        )'''))
        c.execute(text('''CREATE TABLE IF NOT EXISTS logic_settings (
            logic_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL
        )'''))
        c.execute(text('''CREATE TABLE IF NOT EXISTS active_logic_state (
            slot INTEGER PRIMARY KEY, logic_id INTEGER NOT NULL, start_result VARCHAR(10)
        )'''))
        state_cols = {col['name'] for col in inspect(engine).get_columns('active_logic_state')}
        if 'start_result' not in state_cols:
            c.execute(text('ALTER TABLE active_logic_state ADD COLUMN start_result VARCHAR(10)'))
        c.execute(text('''CREATE TABLE IF NOT EXISTS predictions (
            target_issue VARCHAR(80) PRIMARY KEY, prediction VARCHAR(10) NOT NULL,
            actual VARCHAR(10), actual_number INTEGER, result VARCHAR(10) NOT NULL,
            saved_at VARCHAR(40) NOT NULL, colour VARCHAR(10), number_pick INTEGER,
            consensus INTEGER, logic_name VARCHAR(200), logic_accuracy INTEGER
        )'''))
        # Backward-compatible migration for existing SQLite/Postgres databases.
        existing = {col['name'] for col in inspect(engine).get_columns('predictions')}
        if 'logic_name' not in existing:
            c.execute(text('ALTER TABLE predictions ADD COLUMN logic_name VARCHAR(200)'))
        if 'logic_accuracy' not in existing:
            c.execute(text('ALTER TABLE predictions ADD COLUMN logic_accuracy INTEGER'))


def size_of(n): return 'BIG' if int(n) >= 5 else 'SMALL'
def colour_of(n, api_color=None):
    c = str(api_color or '').upper()
    if 'GREEN' in c: return 'GREEN'
    if 'RED' in c: return 'RED'
    if 'VIOLET' in c and int(n) == 0: return 'VIOLET'
    return 'GREEN' if int(n) in (1,3,7,9) else ('RED' if int(n) in (0,2,4,6,8) else 'GREEN')


def normalise_api(raw):
    data = raw.get('data') if isinstance(raw, dict) else {}
    if not isinstance(data, dict): data = {}
    items = data.get('list') or raw.get('list') or data.get('records') or raw.get('records') or []
    out=[]
    for x in items:
        try:
            n=int(x.get('number', x.get('Number', x.get('openNumber'))))
            if n < 0 or n > 9: continue
            issue=str(x.get('issueNumber', x.get('issue', x.get('period', ''))))
            if not issue: continue
            size_raw=str(x.get('size') or '').upper()
            size='BIG' if ('BIG' in size_raw or size_raw == 'B') else ('SMALL' if ('SMALL' in size_raw or size_raw == 'S') else size_of(n))
            out.append({'issue':issue,'number':n,'size':size,'colour':colour_of(n,x.get('colour',x.get('color'))),'time':str(x.get('time',x.get('openTime',x.get('createTime',''))) or '')})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda r: int(r['issue']) if r['issue'].isdigit() else r['issue'], reverse=True)
    return out


def majority(values):
    b=sum(1 for x in values if x=='BIG')
    return 'BIG' if b >= len(values)/2 else 'SMALL'
def current_streak(data, key):
    if not data: return 0
    v=data[0][key]; n=0
    for x in data:
        if x[key] != v: break
        n += 1
    return n
def entropy(vals):
    counts=Counter(vals); total=len(vals)
    return sum(-(v/total)*__import__('math').log2(v/total) for v in counts.values()) if total else 0
def next_from_sequence(seq, order):
    if len(seq) <= order: return None
    key=seq[:order]; found=[]
    for i in range(order,len(seq)):
        if seq[i-order:i] == key: found.append(seq[i-order-1])
    return majority(found) if len(found)>=2 else None


def run_single_method(method, data):
    if not data: return 'BIG'
    nums=[x['number'] for x in data]; sizes=[x['size'] for x in data]
    latest=sizes[0] if sizes else 'BIG'; i=method['id']-1
    fam=method['family']; variant=i%11
    win=min(len(data),[8,10,12,15,20,25,30,40,50,75,100][variant])
    s=sizes[:win]; n=nums[:win]
    big=s.count('BIG'); ratio=big/(len(s) or 1); streak=current_streak(data,'size')
    if fam=='Markov Chains':
        order=1+(variant%4); seq=list(reversed(s[:min(500,len(s))]))
        return next_from_sequence(seq,order) or ('BIG' if ratio>=.5 else 'SMALL')
    if fam=='Moving Averages & Momentum':
        short=s[:min(5+variant*2,len(s))]; mid=s[:min(15+variant*3,len(s))]; long=s
        score=((short.count('BIG')/(len(short) or 1)-.5)*.5 + (mid.count('BIG')/(len(mid) or 1)-.5)*.3 + (long.count('BIG')/(len(long) or 1)-.5)*.2)
        return 'BIG' if score>=0 else 'SMALL'
    if fam=='Mean Reversion & Liability':
        edge=abs(ratio-.5); pred='SMALL' if ratio>.5 else 'BIG'
        if edge<.04: pred='SMALL' if latest=='BIG' else 'BIG'
        return pred
    if fam in ('Streak Dynamics','Dynamic Streak Pivot'):
        continue_trend=streak>=2+(variant%4)
        pred=latest if continue_trend else ('SMALL' if latest=='BIG' else 'BIG')
        if fam=='Dynamic Streak Pivot' and streak>=4: pred='SMALL' if latest=='BIG' else 'BIG'
        return pred
    if fam=='Temporal & Modulo Logic':
        issue=str(data[0].get('issue') or '0'); mod=[3,5,7,10,11,13][variant%6]
        v=int(issue[-4:]) if issue[-4:].isdigit() else 0
        return 'BIG' if ((v%mod)+variant)%2==0 else 'SMALL'
    if fam=='Digit Topology':
        counts=[n.count(x) for x in range(10)]
        hot=counts.index(max(counts)); cold=counts.index(min(counts)); pick=hot if variant%2 else cold
        return 'BIG' if pick>=5 else 'SMALL'
    if fam=='Quantum Entropy':
        return ('SMALL' if latest=='BIG' else 'BIG') if entropy(s)>.95 else majority(s)
    if fam=='Bayesian Posterior':
        alpha=1+big; beta=1+(len(s)-big)
        return 'BIG' if alpha/(alpha+beta)>=.5 else 'SMALL'
    if fam=='N-Gram Memory & Spectral':
        order=2+(variant%4); seq=list(reversed(s[:min(500,len(s))]))
        return next_from_sequence(seq,order) or (majority(s) if variant%2 else ('SMALL' if latest=='BIG' else 'BIG'))
    return majority(s)


def calculate_accuracy(method, data):
    # Accuracy on the 100 most recent completed periods.
    correct = evaluated = 0
    for step in range(1, min(101, len(data))):
        if run_single_method(method, data[step:]) == data[step - 1]['size']:
            correct += 1
        evaluated += 1
    return round(correct / evaluated * 100) if evaluated else 0


def get_enabled_methods():
    with engine.connect() as c:
        disabled = {int(r['logic_id']) for r in c.execute(text('SELECT logic_id FROM logic_settings WHERE enabled=0')).mappings().all()}
    return [m for m in METHODS if int(m['id']) not in disabled]


def get_active_logic_id():
    with engine.connect() as c:
        row = c.execute(text('SELECT logic_id FROM active_logic_state WHERE slot=1')).first()
    return int(row[0]) if row else None


def get_active_start_result():
    with engine.connect() as c:
        row = c.execute(text('SELECT start_result FROM active_logic_state WHERE slot=1')).first()
    return row[0] if row else None


def set_active_logic_id(logic_id, reset_start=True):
    with engine.begin() as c:
        c.execute(text('''INSERT INTO active_logic_state(slot,logic_id,start_result) VALUES(1,:id,NULL)
            ON CONFLICT(slot) DO UPDATE SET logic_id=excluded.logic_id,
            start_result=CASE WHEN :reset_start=1 THEN NULL ELSE active_logic_state.start_result END'''),
            {'id': int(logic_id), 'reset_start': 1 if reset_start else 0})


def set_active_start_result(result):
    with engine.begin() as c:
        c.execute(text('UPDATE active_logic_state SET start_result=:result WHERE slot=1'), {'result': result})


def choose_lowest_accuracy_logic(data, active_methods):
    if not active_methods:
        return None
    ranked = [{**method, 'accuracy': calculate_accuracy(method, data)} for method in active_methods]
    return min(ranked, key=lambda item: (item['accuracy'], item['id']))


def make_prediction(data, selected_method=None):
    # 100 completed periods plus the current latest result are required.
    if len(data) < 101:
        return None
    active_methods = get_enabled_methods()
    if not active_methods:
        return None
    if selected_method is None:
        selected_method = choose_lowest_accuracy_logic(data, active_methods)
    if selected_method is None:
        return None
    # The active logic stays locked until the first result opposite to its first settled result.
    best = {**selected_method, 'accuracy': calculate_accuracy(selected_method, data)}
    size = run_single_method(best, data)
    freq = [0] * 10
    for row in data[:100]:
        freq[row['number']] += 1
    recent_colours = [row['colour'] for row in data[:20]]
    green = sum(1 for colour in recent_colours if colour == 'GREEN')
    colour = 'GREEN' if green >= len(recent_colours) / 2 else 'RED'
    candidates = [n for n in range(10) if size_of(n) == size and colour_of(n) == colour]
    if not candidates:
        candidates = [n for n in range(10) if size_of(n) == size]
    number_pick = sorted(candidates, key=lambda n: freq[n], reverse=True)[0] if candidates else (7 if size == 'BIG' else 2)
    issue = str(data[0]['issue'])
    next_issue = str(int(issue) + 1) if issue.isdigit() else '--'
    return {
        'size': size, 'colour': colour, 'number': number_pick, 'nextIssue': next_issue,
        'consensus': best['accuracy'], 'bigVotes': 1 if size == 'BIG' else 0,
        'smallVotes': 1 if size == 'SMALL' else 0, 'activeMethods': 1,
        'useAccuracyMode': True, 'topAccuracy': best['accuracy'],
        'votingRule': 'lock_until_opposite_of_first_result_then_lowest_accuracy_last_100', 'votingMethods': [best['name']],
        'logicId': best['id'], 'logicName': best['name'], 'logicAccuracy': best['accuracy'],
        'votingMethodDetails': [{'id': best['id'], 'name': best['name'],
                                 'accuracy': best['accuracy'], 'prediction': size}]
    }


def db_rows(limit=1500):
    # Period IDs are fixed-width numeric strings in this feed, so descending SQL order
    # lets the collector read only the recent window instead of loading the full DB each cycle.
    with engine.connect() as c:
        if limit is None:
            rows=c.execute(text('SELECT issue, number, size, colour, draw_time FROM results')).mappings().all()
        else:
            rows=c.execute(text('SELECT issue, number, size, colour, draw_time FROM results ORDER BY issue DESC LIMIT :lim'), {'lim': int(limit)}).mappings().all()
    out=[{'issue':r['issue'],'number':r['number'],'size':r['size'],'colour':r['colour'],'time':r['draw_time'] or ''} for r in rows]
    out.sort(key=lambda r: int(r['issue']) if str(r['issue']).isdigit() else str(r['issue']), reverse=True)
    return out if limit is None else out[:limit]


def fetch_latest_api_items():
    headers={'Accept':'application/json','User-Agent':'Mozilla/5.0 (compatible; WinGoHistoryCollector/2.0)','Connection':'close'}
    last_error = None
    for attempt in range(API_RETRIES):
        try:
            response=requests.get(API_BASE.format(page=1), timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT), headers=headers)
            response.raise_for_status()
            items=normalise_api(response.json())
            if not items:
                raise RuntimeError('API response did not contain valid result rows')
            return items
        except Exception as exc:
            last_error = exc
            logging.warning('Upstream API attempt %s/%s failed: %s', attempt + 1, API_RETRIES, exc)
            if attempt + 1 < API_RETRIES:
                time.sleep(min(2 ** attempt, 4))
    raise RuntimeError(f'Upstream API unavailable after {API_RETRIES} attempts: {last_error}')


def persist_batch(items):
    now=datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        for r in items:
            c.execute(text('''INSERT INTO results(issue,number,size,colour,draw_time,saved_at)
                VALUES(:issue,:number,:size,:colour,:time,:saved)
                ON CONFLICT(issue) DO UPDATE SET number=excluded.number,size=excluded.size,colour=excluded.colour,draw_time=excluded.draw_time'''),
                {'issue':r['issue'],'number':r['number'],'size':r['size'],'colour':r['colour'],'time':r['time'],'saved':now})



def process_cycle():
    # Never let two collector cycles overlap (e.g. after a slow upstream response).
    if not cycle_lock.acquire(blocking=False):
        logging.warning('Collector cycle skipped: previous cycle is still running')
        return
    try:
        with collector_status_lock:
            collector_status['last_attempt'] = datetime.now(timezone.utc).isoformat()
        items = fetch_latest_api_items()
        persist_batch(items)
        with collector_status_lock:
            collector_status['last_success'] = datetime.now(timezone.utc).isoformat()
            collector_status['last_error'] = None
            collector_status['consecutive_failures'] = 0
            collector_status['last_rows'] = len(items)
        rows=db_rows(1500)
        actual_map={r['issue']:r for r in rows}
        now=datetime.now(timezone.utc).isoformat()
        # Always settle older predictions first, even while waiting for enough accuracy data.
        newly_lost = False
        with engine.begin() as c:
            pending=c.execute(text("SELECT target_issue,prediction,logic_name FROM predictions WHERE result='PENDING'")).mappings().all()
            settled_events = []
            for old in pending:
                actual=actual_map.get(old['target_issue'])
                if actual:
                    result='WIN' if old['prediction']==actual['size'] else 'LOSS'
                    c.execute(text("UPDATE predictions SET actual=:a,actual_number=:n,result=:r WHERE target_issue=:i"),
                        {'a':actual['size'],'n':actual['number'],'r':result,'i':old['target_issue']})
                    settled_events.append({'result': result, 'logic_name': old['logic_name']})

        active_methods = get_enabled_methods()
        if not active_methods:
            logging.info('Collector cycle: all logics disabled')
            return
        active_ids = {int(m['id']) for m in active_methods}
        selected_id = get_active_logic_id()
        # Lock rule: remember the first settled outcome for the selected logic.
        # Re-select only when that logic later gets the opposite outcome.
        active_method = next((m for m in active_methods if int(m['id']) == selected_id), None)
        active_name = active_method['name'] if active_method else None
        start_result = get_active_start_result()
        switch_now = selected_id not in active_ids
        if not switch_now and active_name:
            for event in settled_events:
                if event['logic_name'] != active_name:
                    continue
                if start_result is None:
                    start_result = event['result']
                    set_active_start_result(start_result)
                elif event['result'] != start_result:
                    switch_now = True
                    break
        if switch_now:
            selected = choose_lowest_accuracy_logic(rows, active_methods) if len(rows) >= 101 else None
            if selected:
                selected_id = int(selected['id'])
                set_active_logic_id(selected_id, reset_start=True)
        selected_method = next((m for m in active_methods if int(m['id']) == selected_id), None)
        p=make_prediction(rows, selected_method)
        if not p or p['nextIssue']=='--':
            reason = 'all logics disabled' if not get_enabled_methods() else 'waiting for 101 records'
            logging.info('Collector cycle: fetched=%s latest=%s; %s',len(items),items[0]['issue'],reason)
            return
        with engine.begin() as c:
            c.execute(text('''INSERT INTO predictions(target_issue,prediction,actual,actual_number,result,saved_at,colour,number_pick,consensus,logic_name,logic_accuracy)
                VALUES(:issue,:prediction,NULL,NULL,'PENDING',:saved,:colour,:number,:consensus,:logic_name,:logic_accuracy)
                ON CONFLICT(target_issue) DO UPDATE SET prediction=excluded.prediction,actual=NULL,actual_number=NULL,result='PENDING',saved_at=excluded.saved_at,colour=excluded.colour,number_pick=excluded.number_pick,consensus=excluded.consensus,logic_name=excluded.logic_name,logic_accuracy=excluded.logic_accuracy WHERE predictions.result='PENDING' '''),
                {'issue':p['nextIssue'],'prediction':p['size'],'saved':now,'colour':p['colour'],'number':p['number'],'consensus':p['consensus'],'logic_name':p['logicName'],'logic_accuracy':p['logicAccuracy']})
        logging.info('Collector cycle: fetched=%s latest=%s next_prediction=%s %s selected_logic=%s accuracy=%s switched_after_opposite_result=%s',
            len(items),items[0]['issue'],p['nextIssue'],p['size'],p['logicName'],p['logicAccuracy'],switch_now)
    finally:
        cycle_lock.release()

def collector_loop():
    # One initial cycle, then every 3 seconds by default. A platform restart resets this timer.
    while True:
        started=time.time()
        try:
            process_cycle()
        except Exception as exc:
            logging.exception('Collector error: %s',exc)
            with collector_status_lock:
                collector_status['last_error'] = str(exc)[:500]
                collector_status['consecutive_failures'] += 1
        with collector_status_lock:
            failures = collector_status['consecutive_failures']
        target_delay = min(POLL_SECONDS * (2 ** min(failures, 4)), 30) if failures else POLL_SECONDS
        time.sleep(max(1, target_delay-(time.time()-started)))


@app.after_request
def add_api_headers(response):
    if request.path.startswith('/api/'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept'
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response


@app.get('/')
def home(): return send_from_directory('static','index.html')

@app.get('/api/logics')
def get_logics():
    enabled_ids = {int(m['id']) for m in get_enabled_methods()}
    return jsonify({'logics': [{'id': int(m['id']), 'name': m['name'], 'family': m['family'], 'enabled': int(m['id']) in enabled_ids} for m in METHODS], 'enabledCount': len(enabled_ids), 'totalCount': len(METHODS)})


@app.post('/api/logics/toggle')
def toggle_logic():
    payload = request.get_json(silent=True) or {}
    try:
        logic_id = int(payload.get('id'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Valid logic ID is required'}), 400
    if logic_id not in {int(m['id']) for m in METHODS}:
        return jsonify({'ok': False, 'error': 'Unknown logic ID'}), 404
    enabled = bool(payload.get('enabled'))
    with engine.begin() as c:
        c.execute(text('INSERT INTO logic_settings(logic_id,enabled) VALUES(:id,:enabled) ON CONFLICT(logic_id) DO UPDATE SET enabled=excluded.enabled'), {'id': logic_id, 'enabled': 1 if enabled else 0})
    return jsonify({'ok': True, 'id': logic_id, 'enabled': enabled, 'enabledCount': len(get_enabled_methods())})


@app.post('/api/logics/bulk')
def bulk_toggle_logics():
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get('enabled'))
    with engine.begin() as c:
        if enabled:
            c.execute(text('DELETE FROM logic_settings'))
        else:
            c.execute(text('INSERT INTO logic_settings(logic_id,enabled) VALUES(:id,0) ON CONFLICT(logic_id) DO UPDATE SET enabled=0'), [{'id': int(m['id'])} for m in METHODS])
    return jsonify({'ok': True, 'enabled': enabled, 'enabledCount': len(get_enabled_methods())})


@app.get('/health')
def health():
    with collector_status_lock:
        status = dict(collector_status)
    return jsonify({'ok':True,'service':'wingo-collector','poll_seconds':POLL_SECONDS,
                    'upstream':'LIVE' if status['last_success'] and not status['last_error'] else 'WAITING_OR_ERROR',
                    'collector':status})

@app.get('/api/state')
def state():
    rows=db_rows(1500)
    with engine.connect() as c:
        preds=c.execute(text('''SELECT target_issue,prediction,actual,actual_number,result,saved_at,colour,number_pick,consensus,logic_name,logic_accuracy
            FROM predictions ORDER BY target_issue DESC LIMIT 1000''')).mappings().all()
        total=c.execute(text('SELECT COUNT(*) FROM results')).scalar_one()
    predictions=[{'targetIssue':r['target_issue'],'prediction':r['prediction'],'actual':r['actual'] or '--','actualNumber':r['actual_number'],'result':r['result'],'savedAt':r['saved_at'],'colour':r['colour'],'number':r['number_pick'],'consensus':r['consensus'],'logicName':r['logic_name'] or '--','logicAccuracy':r['logic_accuracy']} for r in preds]
    predictions.sort(key=lambda p: int(p['targetIssue']) if str(p['targetIssue']).isdigit() else str(p['targetIssue']), reverse=True)
    # Serve the prediction already calculated by the background collector.
    # Do not rerun all 108 logic evaluations on every dashboard refresh.
    latest = rows[0] if rows else None
    latest_issue_num = int(latest['issue']) if latest and str(latest['issue']).isdigit() else None
    latest_prediction = next((p for p in predictions if p['result'] == 'PENDING' and
                              (latest_issue_num is None or not str(p['targetIssue']).isdigit() or int(p['targetIssue']) > latest_issue_num)), None)
    current_engine = None
    if latest_prediction:
        current_engine = {
            'size': latest_prediction['prediction'],
            'colour': latest_prediction['colour'],
            'number': latest_prediction['number'],
            'nextIssue': latest_prediction['targetIssue'],
            'consensus': latest_prediction['consensus'] or 0,
            'bigVotes': 1 if latest_prediction['prediction'] == 'BIG' else 0,
            'smallVotes': 1 if latest_prediction['prediction'] == 'SMALL' else 0,
            'activeMethods': 1, 'useAccuracyMode': True,
            'topAccuracy': latest_prediction['logicAccuracy'] or 0,
            'votingRule': 'lock_until_opposite_of_first_result_then_lowest_accuracy_last_100',
            'votingMethods': [latest_prediction['logicName']],
            'logicName': latest_prediction['logicName'],
            'logicAccuracy': latest_prediction['logicAccuracy'],
            'votingMethodDetails': [{
                'name': latest_prediction['logicName'],
                'accuracy': latest_prediction['logicAccuracy'] or 0,
                'prediction': latest_prediction['prediction']
            }]
        }
    # API History must show only the latest 10 actual records returned by the API.
    api_history = rows[:10]
    settled=[p for p in predictions if p['result'] in ('WIN','LOSS')]
    wins=sum(1 for p in settled if p['result']=='WIN')
    with collector_status_lock:
        collector = dict(collector_status)
    return jsonify({'history':api_history,'predictions':predictions,'latestPrediction':latest_prediction,'totalHistory':total,'latest':latest,'stats':{'settled':len(settled),'wins':wins,'losses':len(settled)-wins,'accuracy':round(wins/len(settled)*100) if settled else 0},'serverTime':datetime.now(timezone.utc).isoformat(),'pollSeconds':POLL_SECONDS,'collector':collector,'engine':current_engine})


@app.get('/api/prediction')
def prediction_json():
    """Fast public JSON endpoint; serves the collector's saved prediction."""
    with engine.connect() as c:
        current = c.execute(text("""SELECT target_issue,prediction,actual,actual_number,result,saved_at,colour,number_pick,logic_name,logic_accuracy
            FROM predictions WHERE result='PENDING' ORDER BY target_issue DESC LIMIT 1""")).mappings().first()
        previous = c.execute(text("""SELECT target_issue,prediction,actual,actual_number,result,saved_at,colour,number_pick,logic_name,logic_accuracy
            FROM predictions WHERE result IN ('WIN','LOSS') ORDER BY target_issue DESC LIMIT 1""")).mappings().first()
        latest_result = c.execute(text('SELECT issue FROM results ORDER BY issue DESC LIMIT 1')).first()
        if current and latest_result and str(current['target_issue']).isdigit() and str(latest_result[0]).isdigit() and int(current['target_issue']) <= int(latest_result[0]):
            current = None

    def public_record(r):
        if not r: return None
        return {
            'periodNumber': r['target_issue'],
            'prediction': r['prediction'],
            'status': r['result'],
            'actualResult': r['actual'] or None,
            'actualNumber': r['actual_number'],
            'colourPrediction': r['colour'],
            'numberPrediction': r['number_pick'],
            'logicName': r['logic_name'] or '--',
            'logicAccuracy': r['logic_accuracy'],
            'createdAt': r['saved_at']
        }

    with collector_status_lock:
        collector = dict(collector_status)
    response = jsonify({
        'ok': True,
        'game': 'WinGo_30S',
        'currentPrediction': public_record(current),
        'previousPrediction': public_record(previous),
        'collector': collector,
        'updatedAt': datetime.now(timezone.utc).isoformat()
    })
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept'
    return response


@app.get('/api/predictions.csv')
def predictions_csv():
    """Download the complete saved prediction history as UTF-8 CSV."""
    with engine.connect() as c:
        rows = c.execute(text('''SELECT target_issue,prediction,actual,actual_number,result,colour,number_pick,logic_name,logic_accuracy,saved_at FROM predictions ORDER BY target_issue DESC''')).mappings().all()
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['Period Number', 'Prediction', 'Actual Result', 'Actual Number', 'WIN/LOSS Status', 'Colour Prediction', 'Number Prediction', 'Logic Used', 'Logic Accuracy (%)', 'Prediction Time'])
    for r in rows:
        writer.writerow([r['target_issue'], r['prediction'], r['actual'] or '', r['actual_number'] if r['actual_number'] is not None else '', r['result'], r['colour'] or '', r['number_pick'] if r['number_pick'] is not None else '', r['logic_name'] or '', r['logic_accuracy'] if r['logic_accuracy'] is not None else '', r['saved_at']])
    data = '\ufeff' + output.getvalue()
    return Response(data, mimetype='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename=wingo_prediction_history.csv', 'Cache-Control': 'no-store'})


@app.post('/api/predictions/clear')
def clear_prediction_history():
    # Clear generated prediction records only; keep the actual API draw history.
    with engine.begin() as c:
        c.execute(text('DELETE FROM predictions'))
    return jsonify({'ok':True,'message':'Prediction history cleared; API history preserved.'})


init_db()
# Gunicorn should run a single worker (see render.yaml); guard prevents duplicate threads on reloader.
threading.Thread(target=collector_loop,daemon=True,name='wingo-collector').start()
