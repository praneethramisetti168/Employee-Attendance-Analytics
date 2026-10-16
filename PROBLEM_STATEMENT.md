# HROne | Software Engineer Hiring | Contract v2.0
## Engineering Assignment: Employee Attendance & Analytics API (FastAPI + MongoDB)

---

### 1. Overview
You will build the backend of a small HR attendance system: employees punch in and out, managers correct records, and the business asks questions of the data (who is late, which department is slipping, what does the trend look like). The data lives in MongoDB. You write the service in FastAPI.

This is not a tutorial. The contract is precise, the business rules are written in prose, and the dataset we test with is larger and nastier than the sample we give you. Plausible-looking output is not the same as correct output. Please read everything before you start.

| Part | Guide time | What you do |
| :--- | :--- | :--- |
| **A. Review** | 45 min | A colleague left a half-finished `app/main.py`. It runs, but it has not been reviewed. Find what is wrong and fix it. Record your findings in `REVIEW.md`. |
| **B. Build** | 2.5 - 3 h | Implement the remaining endpoints, including three non-trivial MongoDB aggregation pipelines. |
| **C. Prove & explain** | 30 min | Add an explain endpoint that proves your queries use indexes. Answer five questions in `DECISIONS.md`. |

**Stack:** Python 3.11+, FastAPI, PyMongo or Motor, MongoDB 6.0+ (Atlas free tier or local Docker).  
**Total guide time:** about 4 to 5 hours.

---

### 2. What you receive and what you submit

#### You receive (candidate kit):
* `PROBLEM_STATEMENT.docx`: This document.
* `openapi.yaml`: The contract: paths, parameters, status codes, response fields and the business rules R1 - R10.
* `app/main.py`: The starter application (Part A).
* `DATA_MODEL.md`: The stored data: collections, field types, rules for seeded data, and nine annotated sample documents.
* `sample_data/`, `sample_seed.py`: The nine sample documents as MongoDB Extended JSON, and a script that loads them into your database.
* `REVIEW.md`, `DECISIONS.md`: Templates to fill in.

#### You submit (public Git repository):
* `app/main.py`: All application code in this one file.
* `requirements.txt`: At the repository root. Include everything you import.
* `REVIEW.md`, `DECISIONS.md`: Parts A and C.
* `README.md`: A few lines: how to run, anything we should know.
* **Do not include:** `.env`, secrets, a Dockerfile, virtual environments, or data dumps.

---

### 3. Ground rules
* Documentation, search engines and your own notes are fine. If you use AI tools, treat them like any other reference: you are responsible for every line.
* Shortlisted candidates are invited to a live session where you walk through your code, explain choices from `DECISIONS.md`, and make a small change on the spot.
* Submissions are first evaluated automatically. Follow the contract exactly: a wrong field name or status code is a failed test, not a debate.
* Work alone. Submissions are checked for similarity against each other.

---

### 4. Environment contract
* The app starts with `uvicorn app.main:app --port 8000` from the repository root and must be ready (`GET /health` returns 200) within 20 seconds.
* Read `MONGO_URI` and `MONGO_DB` from environment variables. A local `.env` is fine, but real environment variables must take priority. We inject our own values; never hard-code connection details.
* Create all indexes at startup, idempotently. The grader starts your app against a database that already holds the hidden dataset.
* No network access other than MongoDB. Do not call external services.
* The app must handle concurrent requests correctly; we send parallel requests.

---

### 5. Data model and conventions
MongoDB holds two collections, `employees` and `attendance_logs`.

| Convention | What it means |
| :--- | :--- |
| **Employee identity** | `emp_code` identifies an employee everywhere (e.g. `EMP0001`): unique, never changes, and not MongoDB's `_id`. `_id` is internal and never appears in a request or response. Attendance records have no id either; a record is addressed by `(emp_code, date)`. |
| **Instants** | Every point in time in the API (`punched_at`, `punch_in`, `punch_out`, `created_at`, and the times in history) is an integer in epoch milliseconds. MongoDB stores them as BSON datetimes (UTC), so you convert at the boundary. Values that look like seconds, floats and strings are rejected with 422. |
| **Calendar values** | Dates are `YYYY-MM-DD` (IST), months are `YYYY-MM`, and shift times are `HH:MM` (24-hour, IST), both in the API and in MongoDB. |
| **Derived fields** | `work_hours`, `late_minutes`, `overtime_minutes` and `half_day` are computed by your API and never sent by a client. They are stored, and analytics read the stored values instead of recomputing them. |
| **Gaps in the data** | Seeded records may omit history or half_day; treat them as empty and false. A weekday with no record at all is not the same as a record with status `ABSENT`. |
| **Indexes** | The seeded database has only the default `_id` index. Create the ones you need at startup. |

#### Status values:
* `PRESENT`: Worked on site (`punch_in` required; counts as present)
* `WFH`: Worked from home (`punch_in` required; counts as present)
* `ON_DUTY`: Official duty away from office (`punch_in` required; counts as present)
* `ABSENT`: Did not work, not on approved leave (`punch_in`/`punch_out` null; not present)
* `LEAVE`: On approved leave (`punch_in`/`punch_out` null; not present)

---

### 6. The Business Rules (R1 – R10)
* **R1 Time zone and attendance date:** Shift times and attendance dates are IST (UTC+05:30). A record's `date` is the IST calendar date of its `punch_in` instant. Exception - overnight shifts (`shift_end <= shift_start`, e.g. 22:00-06:00): a punch-in whose IST time is earlier than `shift_end` belongs to the shift that started the previous day. Instants are truncated to whole seconds.
* **R2 Late:** Grace period 10 minutes. Late only if the punch-in is strictly more than 10:00 after `shift_start`; then `late_minutes = whole minutes (floor)` since `shift_start`, not since the grace period ended. Shift 09:30: 09:40:00 gives 0; 09:40:01 gives 10; 10:15:59 gives 45.
* **R3 Overtime:** Whole minutes (floor) from `shift_end` to `punch_out`, counted only if that is at least 30; otherwise 0.
* **R4 Work hours:** `(punch_out - punch_in)` in seconds / 3600, rounded to 2 decimals half-up. null until punch-out.
* **R5 Half day:** After punch-out, `half_day` is true when the rounded `work_hours < 4.50`. A half day counts as 0.5 of a present day.
* **R6 Presence:** `PRESENT`, `WFH`, `ON_DUTY` count as present. `ABSENT`, `LEAVE` do not.
* **R7 Working days:** Monday - Friday, no holiday calendar; counted from `joined_on` for mid-month joiners. `present_days` counts only Mon - Fri records; weekend records still add to late and overtime totals.
* **R8 Reported numbers:** 2 decimals half-up (rates: 4 decimals half-up).
* **R9 Headcount:** Employees with `joined_on` on or before the end of the period, whether or not they have any logs.
* **R10 Pagination:** `page` starts at 1; `page_size` default 20, max 100; `total` always reflects the filters.

---

### 7. The API at a glance

| Endpoint | Summary | Where |
| :--- | :--- | :--- |
| `GET /health` | 200 only when MongoDB answers; else 503 | Starter |
| `POST /employees` | Create; 409 on duplicate emp_code; 422 on invalid input | Starter |
| `GET /employees` | Filter by department; paginated; sorted by emp_code | Starter |
| `POST /attendance/punch-in` | Creates the day's record (R1, R2); status PRESENT, WFH or ON_DUTY; race-safe | Starter |
| `GET /attendance` | Filter by employee / dates / status; paginated; fast at 100k docs | Starter |
| `POST /attendance/punch-out` | Computes work hours, overtime, half day (R3 - R5); works across midnight | You |
| `PATCH /attendance/{emp_code}/{date}` | Regularize one day's record: recompute derived fields, append to history | You |
| `GET /analytics/employees/{emp_code}/monthly` | Monthly summary for one employee (aggregation) | You |
| `GET /analytics/departments/summary` | Department summary for a month (aggregation, cross-collection) | You |
| `GET /analytics/leaderboard/late` | Late-comers ranking with ties (aggregation, window function) | You |
| `GET /analytics/departments/{department}/trend` | Daily series with gap filling and a 7-day moving average | You |
| `GET /admin/explain/{endpoint}` | MongoDB explain output for an endpoint's main query | You |

#### The three hard endpoints:
* **Department summary:** Logs reference employees by `emp_code`, but the department lives on the employee. Headcount must include people with no logs and exclude people who have not joined yet. The average is over records, not an average of averages.
* **Late leaderboard:** Ties share a rank and the next rank is skipped (1, 2, 2, 4). `limit` is applied after ranking. Tied rows are ordered by `emp_code`. Sorting, limiting and using the array index as the rank is the obvious approach, and it is wrong.
* **Department trend:** Exactly one row per calendar day in the range, including weekends and days with no records, each with a moving average over the preceding window. The first rows have a shorter window. The work is done in the database, not in a Python loop.

---

### 8. Evaluation Criteria

| Area | Weight | How it is checked |
| :--- | :--- | :--- |
| **Review (Part A)** | 10% | `REVIEW.md` against known defects; fixes exercised by automated tests. |
| **CRUD, rules and concurrency** | 30% | Automated tests on status codes, R1 - R5, pagination, race conditions. |
| **Analytics correctness** | 30% | Automated comparison against expected results on hidden dataset (ties, no-log employees, weekends, joiners, overnight shifts). |
| **MongoDB usage and speed** | 15% | Explain endpoint plans, indexes, and performance on ~100,000 records. |
| **Decisions and code quality** | 15% | `DECISIONS.md`, readability, structure inside a single file, input validation. |
