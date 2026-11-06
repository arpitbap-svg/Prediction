WinGo 30S Cloud Collector — Render Deployment
==============================================

Is project mein:
- Python server WinGo_30S API ko default har 30 second mein poll karta hai.
- Result aur 108-method engine ki prediction server-side calculate hoti hai.
- Result/prediction database mein store hoti hai; target period milne par WIN/LOSS settle hota hai.
- Website khuli na ho tab bhi collector server par kaam karta hai, jab tak Render instance chal raha ho.
- Website /api/state se saved history aur predictions dikhati hai.

IMPORTANT FREE-PLAN LIMITS
--------------------------
Render Free Web Service 15 minute inbound traffic na milne par sleep hoti hai; restart bhi ho sakta hai. cron-job.org se /health par har 5 minute ping dene se idle sleep se bachne mein madad mil sakti hai, lekin 24x7 uptime guarantee nahi hoti. Ping request Python collector ko har 5 minute par hi nahi chalati; collector ka apna server-side loop 30-second interval par hai. Sleep/restart ke dauran loop rukega aur restart par dobara shuru hoga.

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
POLL_SECONDS defaults to 30. Change it only if you intentionally want a different polling interval.
The app uses one Gunicorn worker because the collector runs in a background thread inside the web process.
