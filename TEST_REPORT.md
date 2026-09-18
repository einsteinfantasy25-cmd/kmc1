# KMC Bot — Test Report

Test date: 2026-09-18

## Automated checks completed

- Python syntax compilation: `bot.py` + `keep_alive.py` — PASS.
- 26 deterministic unit/integration/static tests — PASS.
- 24,000 randomized arithmetic/property checks across stages 1–6 and cumulative calculations — PASS.
- Local SQLite schema creation — PASS.
- Migration simulation from the old bot SQLite schema — PASS.
- Stage-result persistence round trip — PASS.
- Report HTML persistence in DB — PASS.
- Broadcast database lifecycle — PASS.
- Maintenance setting persistence — PASS.
- Stage-targeted recipient filtering and banned-user exclusion — PASS.
- Health HTTP endpoint `/health` returned HTTP 200 and expected body — PASS.
- Hard-coded Telegram token scan — PASS (none found).
- Dangerous `eval` / `exec` / shell call scan — PASS (none found).
- Duplicate top-level Python definition scan — PASS.
- Render configuration static checks — PASS.
- Old `python-bidi` / ReportLab build dependency removed — PASS.

## Curriculum assertions tested

- Stage 1 is frozen to the original bot snapshot: 15 subjects / 36 credits / 5%.
- Stage 2: 41 credits / 5%.
- Stage 3: 43 credits / 5%.
- Stage 4: 47 credits / 20%.
- Stage 5: 51 credits / 25%, including exact 3.5-credit subjects.
- Stage 6: 48 credits / 40%.
- Graduation-stage weights total exactly 100%.

## Calculation assertions tested

- A 100 average produces the exact stage contribution for every stage.
- A uniform 80 average in Stage 5 produces 80 stage average and 20 graduation points.
- Stage 6 single 12-credit subject at 100 and all others 0 produces 25 stage average and 10 graduation points.
- Grade-range mode produces correct weighted min/max ranges.
- Exact cumulative formula and range cumulative formula were both tested.
- Invalid numeric values (`NaN`, infinity, <0, >100, text) are rejected.
- Credit tampering/mismatch raises an error instead of returning a wrong result.

## Environment-dependent tests not executable here

A real Telegram API session and a real remote PostgreSQL server require your private `BOT_TOKEN` and `DATABASE_URL`, so they were not used in the offline test environment. The SQLite database path and PostgreSQL SQL-placeholder/backend-selection logic were tested; deployment should still be verified once with your actual credentials before replacing the live bot permanently.
