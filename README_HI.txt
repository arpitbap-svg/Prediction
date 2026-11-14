WinGo 30S Cloud Collector — Render Deployment
==============================================

Is project mein:
- Python server WinGo_30S API ko default har 3 second mein poll karta hai. Timeout par 3 attempts (connect/read timeout ke saath) aur automatic backoff retry hota hai.
- Result aur 108-method engine ki prediction server-side calculate hoti hai.
- Result/prediction database mein store hoti hai; target period milne par WIN/LOSS settle hota hai.
- Website khuli na ho tab bhi collector server par kaam karta hai, jab tak Render instance chal raha ho.
- Website /api/state se saved history aur predictions dikhati hai.

IMPORTANT FREE-PLAN LIMITS
--------------------------
Render Free Web Service 15 minute inbound traffic na milne par sleep hoti hai; restart bhi ho sakta hai. cron-job.org se /health par har 5 minute ping dene se idle sleep se bachne mein madad mil sakti hai, lekin 24x7 uptime guarantee nahi hoti. Ping request Python collector ko har 5 minute par hi nahi chalati; collector ka apna server-side loop 3-second interval par hai. Sleep/restart ke dauran loop rukega aur restart par dobara shuru hoga.

Render Free filesystem ephemeral hai. Isliye long-term history ke liye external PostgreSQL DB (jaise Supabase project ka Postgres connection string) lagana zaroori hai. Agar DATABASE_URL set nahi karoge to local SQLite fallback chalega, lekin Render restart/redeploy/sleep par data kho sakta hai. Render ka Free Postgres bhi 30 din baad expire hota hai.

DEPLOY STEPS
------------
1. Is folder ke sabhi files GitHub repository mein upload karo.
2. Render.com > New > Blueprint ya New Web Service > GitHub repo select karo.
3. Blueprint use karoge to render.yaml config read hoga. Service ka plan Free rakho.
4. Supabase (ya kisi external PostgreSQL provider) mein database banao aur PostgreSQL connection URI lo.
5. Render Dashboard > service > Environment mein DATABASE_URL add karo. URI ko secret rakho; kisi ko share mat karna.
6. Deploy complete hone ke baad https://YOUR-SERVICE.onrender.com/health kholo; ok:true aana chahiye.
7. Cron-job.org mein ek job banao: URL https://YOUR-SERVICE.onrender.com/health ; schedule every 5 minutes; method GET; save/enable.
8. Main website URL kholo. Dashboard saved results aur prediction history load karega.

IMPORTANT:
- Cron ping is only a keep-awake attempt. Render can still restart/suspend free instances and the monthly 750 free instance-hours limit applies. Render may suspend unusually high service-initiated external traffic. Therefore this is best-effort, not guaranteed 24x7.
- API endpoint currently seeds the latest 200 records at first run; afterwards it stores each newly seen record on every poll. It cannot reconstruct draws missed while the service is asleep if those draws fall outside the API's returned page window.
- WinGo predictions are statistical guesses, not guaranteed outcomes. Do not use them as a guarantee or financial advice.

CONFIG
------
POLL_SECONDS defaults to 3 seconds (minimum 3). API_CONNECT_TIMEOUT defaults to 4 seconds, API_READ_TIMEOUT to 7 seconds, and API_RETRIES to 3 attempts. Upstream failure par retry interval automatically badhkar maximum 30 seconds hota hai; successful fetch ke baad normal polling resume hoti hai.
The app uses one Gunicorn worker because the collector runs in a background thread inside the web process.


Prediction engine update (Nov 2026):
- Family voting has been removed.
- पहली बार 108 enabled logics में से last 100 completed periods पर सबसे कम accuracy वाला एक logic चुना जाता है (tie में छोटा ID)।
- चुने हुए logic की पहली settled prediction का परिणाम (WIN या LOSS) lock का आधार बनेगा।
- यदि पहली settled prediction LOSS है, तो उसी logic से तब तक prediction जारी रहेगी जब तक पहली WIN न आ जाए।
- यदि पहली settled prediction WIN है, तो उसी logic से तब तक prediction जारी रहेगी जब तक पहली LOSS न आ जाए।
- पहली बार विपरीत परिणाम मिलते ही सभी enabled logics का last 100 completed periods पर दोबारा analysis होगा और सबसे कम accuracy वाला logic चुना जाएगा।
- चुना हुआ logic database में save रहता है, इसलिए server restart के बाद भी selection बना रहता है।
- At least 101 stored result rows are required (100 completed periods plus the latest result).
- API result history and saved prediction history remain separate; clearing prediction history does not delete API results.


Naya update: Prediction History mein har WIN/LOSS ke saamne selected logic ka naam aur us logic ki last-100-period accuracy save aur display hoti hai. Purane records ke liye logic details -- dikhegi; naye predictions ke liye details record hongi. Database migration automatic hai; database delete na karein.


NEW API & CSV FEATURES
----------------------
- JSON prediction endpoint: https://YOUR-SERVICE.onrender.com/api/prediction
  Returns currentPrediction (periodNumber, prediction, PENDING status) and previousPrediction (periodNumber, prediction, actualResult, WIN/LOSS status), with logic details.
- CSV download endpoint: https://YOUR-SERVICE.onrender.com/api/predictions.csv
  Downloads the complete saved Prediction History as wingo_prediction_history.csv.
- Website Prediction History section also has a Download CSV button and a direct JSON API link.

LOGIC MANAGER UPDATE (Nov 2026)
-------------------------------
- Website ke bottom mein Dashboard aur Logic Manager tabs diye gaye hain.
- Logic Manager mein 108 methods ko name ya ID se search karke individual ON/OFF kar sakte hain.
- OFF methods ki accuracy backtest aur prediction calculation dono se exclude hoti hai.
- All ON / All OFF controls bhi available hain.
- Settings logic_settings database table mein save hoti hain; redeploy ke baad bhi settings rahengi, jab tak wahi persistent DATABASE_URL use ho.
- Agar saare logics OFF honge to nayi prediction nahi banegi. Pehle se issue hui prediction history delete nahi hoti.
- API endpoints: GET /api/logics, POST /api/logics/toggle, POST /api/logics/bulk.


Performance/API update:
- /api/prediction now reads the latest saved pending prediction directly from the database; it does not recalculate all logics for every API request.
- Dashboard state also reuses the collector's saved prediction.
- Public API responses include CORS and no-cache headers.
- Upstream WinGo request uses a shorter connect/read timeout so a stalled source does not block a collector cycle for 20 seconds.


Logic switching update:
- The first settled result (WIN or LOSS) of a newly selected logic establishes its lock direction.
- If that first result is LOSS, keep the same logic until its first WIN. If it is WIN, keep the same logic until its first LOSS.
- On the first opposite result, re-evaluate enabled logics over the last 100 completed periods and select the single lowest-accuracy logic.
- The active logic ID is stored in active_logic_state and survives redeploy/restart when the same DATABASE_URL is retained.


TIMEOUT / STALE-PREDICTION SAFEGUARDS (Nov 2026)
------------------------------------------------
- Upstream request par bounded connect/read timeout aur up to 3 automatic attempts.
- Repeated source failure par retry backoff maximum 30 seconds tak; successful fetch ke baad normal 3-second polling.
- Collector cycles overlap nahi karte; ek slow cycle ke dauran doosra cycle start nahi hota.
- Har cycle mein recent 1,500 saved results hi load hote hain; dashboard state recent rows/predictions return karti hai, full CSV export alag se complete rehta hai.
- /health aur /api/state collector ka last success, last attempt, error, consecutive failures aur returned row count dikhate hain.
- Purani pending prediction ko current prediction ke roop mein nahi dikhaya jayega agar uska target period latest actual result ke barabar ya usse purana ho.
- Browser dashboard request 8 seconds ke baad abort hoti hai aur next 3-second polling cycle mein retry karti hai; last rendered data screen par rehta hai.
- Important: Render Free service inactivity ke baad sleep ho sakti hai. Dashboard band hone par external uptime ping (e.g. cron-job.org, every 5 minutes) configure karein, ya always-on paid instance use karein. Koi code Free-plan sleep/restarts ya third-party WinGo API outage ko 100% eliminate nahi kar sakta.
