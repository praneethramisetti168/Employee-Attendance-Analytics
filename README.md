# Employee Attendance & Analytics API

FastAPI + MongoDB attendance service built for the HROne Software Engineer Trainee assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0 or newer (local Docker or MongoDB Atlas free tier)

## Setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

Create a `.env` file at the repository root (real environment variables take priority):

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Load the sample data (optional):

```bash
python sample_seed.py
```

## Run

```bash
uvicorn app.main:app --port 8000
```

The API starts on http://localhost:8000. Interactive docs are at http://localhost:8000/docs.

All indexes are created automatically at startup — no manual steps needed.

## What is implemented

- `GET  /health` — readiness probe (pings MongoDB; 503 if unavailable)
- `POST /employees` — create employee (race-safe unique emp_code)
- `GET  /employees` — list with department filter + pagination, sorted by emp_code
- `POST /attendance/punch-in` — R1, R2; race-safe via unique index
- `POST /attendance/punch-out` — R3, R4, R5; works across midnight overnight shifts; race-safe
- `GET  /attendance` — filter by employee/dates/status; index-backed; correct pagination
- `PATCH /attendance/{emp_code}/{date}` — regularize with audit trail; race-safe history append
- `GET  /analytics/employees/{emp_code}/monthly` — MongoDB aggregation; R5-R8
- `GET  /analytics/departments/summary` — cross-collection aggregation; headcount includes zero-log employees
- `GET  /analytics/leaderboard/late` — $setWindowFields $rank for correct tie handling
- `GET  /analytics/departments/{department}/trend` — $densify gap-filling, 7-day moving average
- `GET  /admin/explain/{endpoint}` — MongoDB executionStats explain for each main query

## Notes

- All epoch millisecond inputs are validated (must be >= 100_000_000_000; rejects seconds, floats, strings).
- Half-up rounding implemented via Python `Decimal` (not built-in `round()` which uses banker's rounding).
- IST timezone handling: all instants converted to UTC for storage; IST used only for date/late-minute calculations.
