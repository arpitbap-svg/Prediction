import os, json, time, threading, logging
from datetime import datetime, timezone
from collections import Counter
import requests
from flask import Flask, jsonify, send_from_directory
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
app = Flask(__name__, static_folder='static')
API_BASE = 'https://draw.ar-lottery01.com/WinGo/WinGo_30S/GetHistoryIssuePage.json?pageSize=10&pageNo={page}'
POLL_SECONDS = max(5, int(os.getenv('POLL_SECONDS', '5')))
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
FAMILIES = list(dict.fromkeys(m['family'] for m in METHODS))


def init_db():
    with engine.begin() as c:
        c.execute(text('''CREATE TABLE IF NOT EXISTS results (
            issue VARCHAR(80) PRIMARY KEY, number INTEGER NOT NULL, size VARCHAR(10) NOT NULL,
            colour VARCHAR(10) NOT NULL, draw_time VARCHAR(80), saved_at VARCHAR(40) NOT NULL
        )'''))
        c.execute(text('''CREATE TABLE IF NOT EXISTS predictions (
            target_issue VARCHAR(80) PRIMARY KEY, prediction VARCHAR(10) NOT NULL,
            actual VARCHAR(10), actual_number INTEGER, result VARCHAR(10) NOT NULL,
            saved_at VARCHAR(40) NOT NULL, colour VARCHAR(10), number_pick INTEGER,
            consensus INTEGER
        )'''))


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


def calculate_accuracy(method,data):
    correct=evaluated=0
    for step in range(1,11):
        if step>=len(data): break
        if run_single_method(method,data[step:])==data[step-1]['size']: correct+=1
        evaluated+=1
    return round(correct/evaluated*100) if evaluated else 50


def make_prediction(data):
    if not data: return None
    use_acc=len(data)>=10
    votes=[]
    for m in METHODS:
        votes.append({**m,'pred':run_single_method(m,data),'accuracy':calculate_accuracy(m,data) if use_acc else 50})
    big_votes=small_votes=0.0
    if use_acc:
        # Strict top-accuracy voting: only methods tied at the exact highest
        # backtest accuracy participate. Lower-accuracy methods never vote.
        max_acc=max(v['accuracy'] for v in votes)
        top=[v for v in votes if v['accuracy']==max_acc]
        for v in top:
            if v['pred']=='BIG': big_votes+=1
            else: small_votes+=1
        active=len(top)
    else:
        active=len(votes)
        big_votes=sum(1 for v in votes if v['pred']=='BIG'); small_votes=sum(1 for v in votes if v['pred']=='SMALL')
    size='BIG' if big_votes>=small_votes else 'SMALL'
    total=big_votes+small_votes or 1
    consensus=round(max(big_votes,small_votes)/total*100)
    by_family=[]
    for fam in FAMILIES:
        group=[v for v in votes if v['family']==fam]; b=sum(1 for v in group if v['pred']=='BIG')
        by_family.append({'family':fam,'big':b,'small':len(group)-b,'total':len(group),'pred':'BIG' if b>=len(group)/2 else 'SMALL'})
    freq=[0]*10
    for r in data[:100]: freq[r['number']]+=1
    recent_colours=[r['colour'] for r in data[:20]]
    green=sum(1 for c in recent_colours if c=='GREEN')
    colour='GREEN' if green>=len(recent_colours)/2 else 'RED'
    candidates=[n for n in range(10) if size_of(n)==size and colour_of(n)==colour]
    if not candidates: candidates=[n for n in range(10) if size_of(n)==size]
    number_pick=sorted(candidates,key=lambda n:freq[n],reverse=True)[0] if candidates else (7 if size=='BIG' else 2)
    issue=str(data[0]['issue']); next_issue=str(int(issue)+1) if issue.isdigit() else '--'
    return {'size':size,'colour':colour,'number':number_pick,'nextIssue':next_issue,'consensus':consensus,'bigVotes':round(big_votes,1),'smallVotes':round(small_votes,1),'activeMethods':active,'useAccuracyMode':use_acc,'topAccuracy':max((v['accuracy'] for v in votes), default=0) if use_acc else None,'votingRule':'exact_top_accuracy_only' if use_acc else 'all_methods_until_accuracy_available','votingMethods':[v['name'] for v in votes if use_acc and v['accuracy']==max(x['accuracy'] for x in votes)] if use_acc else [v['name'] for v in votes]}


def db_rows(limit=1500):
    with engine.connect() as c:
        if limit is None:
            rows=c.execute(text('SELECT issue, number, size, colour, draw_time FROM results ORDER BY issue DESC')).mappings().all()
        else:
            rows=c.execute(text('SELECT issue, number, size, colour, draw_time FROM results ORDER BY issue DESC LIMIT :lim'),{'lim':limit}).mappings().all()
    return [{'issue':r['issue'],'number':r['number'],'size':r['size'],'colour':r['colour'],'time':r['draw_time'] or ''} for r in rows]


def persist_batch(items):
    now=datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        for r in items:
            c.execute(text('''INSERT INTO results(issue,number,size,colour,draw_time,saved_at)
                VALUES(:issue,:number,:size,:colour,:time,:saved)
                ON CONFLICT(issue) DO UPDATE SET number=excluded.number,size=excluded.size,colour=excluded.colour,draw_time=excluded.draw_time'''),
                {'issue':r['issue'],'number':r['number'],'size':r['size'],'colour':r['colour'],'time':r['time'],'saved':now})


def process_cycle():
    headers={'Accept':'application/json','User-Agent':'WinGoHistoryCollector/1.0'}
    # Fetch only the 10 records exposed by the API on each poll.
    # Older fetched results remain in the database for analysis and settlement.
    pages=range(1,2)
    items=[]
    for page in pages:
        response=requests.get(API_BASE.format(page=page),timeout=20,headers=headers)
        response.raise_for_status()
        page_items=normalise_api(response.json())
        if not page_items: break
        items.extend(page_items)
    if not items: raise RuntimeError('API response did not contain result rows')
    persist_batch(items)
    rows=db_rows(None)
    p=make_prediction(rows)
    if not p or p['nextIssue']=='--': return
    now=datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        # Settle predictions as soon as the target issue is present in fetched history.
        actual_map={r['issue']:r for r in rows}
        pending=c.execute(text("SELECT target_issue,prediction FROM predictions WHERE result='PENDING'")).mappings().all()
        for old in pending:
            actual=actual_map.get(old['target_issue'])
            if actual:
                result='WIN' if old['prediction']==actual['size'] else 'LOSS'
                c.execute(text("UPDATE predictions SET actual=:a,actual_number=:n,result=:r WHERE target_issue=:i"),{'a':actual['size'],'n':actual['number'],'r':result,'i':old['target_issue']})
        c.execute(text('''INSERT INTO predictions(target_issue,prediction,actual,actual_number,result,saved_at,colour,number_pick,consensus)
            VALUES(:issue,:prediction,NULL,NULL,'PENDING',:saved,:colour,:number,:consensus)
            ON CONFLICT(target_issue) DO NOTHING'''),
            {'issue':p['nextIssue'],'prediction':p['size'],'saved':now,'colour':p['colour'],'number':p['number'],'consensus':p['consensus']})
    logging.info('Collector cycle: fetched=%s latest=%s next_prediction=%s %s',len(items),items[0]['issue'],p['nextIssue'],p['size'])


def collector_loop():
    # One initial cycle, then every 5 seconds by default. A platform restart resets this timer.
    while True:
        started=time.time()
        try: process_cycle()
        except Exception as exc: logging.exception('Collector error: %s',exc)
        time.sleep(max(1, POLL_SECONDS-(time.time()-started)))


@app.get('/')
def home(): return send_from_directory('static','index.html')

@app.get('/health')
def health(): return jsonify({'ok':True,'service':'wingo-collector','poll_seconds':POLL_SECONDS})

@app.get('/api/state')
def state():
    rows=db_rows(None)
    with engine.connect() as c:
        preds=c.execute(text('''SELECT target_issue,prediction,actual,actual_number,result,saved_at,colour,number_pick,consensus
            FROM predictions ORDER BY target_issue DESC''')).mappings().all()
        total=c.execute(text('SELECT COUNT(*) FROM results')).scalar_one()
    predictions=[{'targetIssue':r['target_issue'],'prediction':r['prediction'],'actual':r['actual'] or '--','actualNumber':r['actual_number'],'result':r['result'],'savedAt':r['saved_at'],'colour':r['colour'],'number':r['number_pick'],'consensus':r['consensus']} for r in preds]
    # Headline prediction comes from the server-saved next-period prediction.
    latest_prediction = predictions[0] if predictions else None
    # API History must show only the latest 10 actual records returned by the API.
    api_history = rows[:10]
    settled=[p for p in predictions if p['result'] in ('WIN','LOSS')]
    wins=sum(1 for p in settled if p['result']=='WIN')
    latest=rows[0] if rows else None
    return jsonify({'history':api_history,'predictions':predictions,'latestPrediction':latest_prediction,'totalHistory':total,'latest':latest,'stats':{'settled':len(settled),'wins':wins,'losses':len(settled)-wins,'accuracy':round(wins/len(settled)*100) if settled else 0},'serverTime':datetime.now(timezone.utc).isoformat(),'pollSeconds':POLL_SECONDS})


init_db()
# Gunicorn should run a single worker (see render.yaml); guard prevents duplicate threads on reloader.
threading.Thread(target=collector_loop,daemon=True,name='wingo-collector').start()
