WINGO RENDER WEBSITE - UPDATED RULES

1. Startup/selection: enabled logics are ranked by accuracy on the recent 100 completed historical periods (the engine retains the existing 101-result requirement to evaluate 100 transitions).
2. The enabled logic with the lowest accuracy is selected; ties are resolved by lower logic ID.
3. The selected logic remains locked until the opposite of its first settled result, then the lowest-accuracy enabled logic is selected again.
4. Every logic's own settled prediction results are monitored. When any logic reaches 6 consecutive WINs or 6 consecutive LOSSES (>5), that logic is automatically switched OFF.
5. On auto-disable, the whole generated Prediction History is cleared, which resets displayed win/loss streak counts. Active logic/lock state is also reset.
6. Actual WinGo draw/API history is NOT deleted. Immediately in the same collector cycle, the remaining enabled logics are re-ranked using the already available recent 100-period draw history and a new prediction is created. There is NO wait for 100 new periods.
7. The manual "Clear Prediction History" button performs the same history/lock reset, preserves actual draw history, and causes logic selection to be recalculated on the next collector cycle.
8. Disabled logics stay disabled until manually enabled again in Logic Manager.

DEPLOY
- Keep static/index.html inside the static folder.
- Upload all project files to GitHub, preserving the folder structure.
- Render start command: gunicorn app:app (see render.yaml).
- Set DATABASE_URL to your persistent PostgreSQL database for state to survive restarts.

Note: Historical accuracy is a statistical backtest, not a guarantee of future outcomes.
