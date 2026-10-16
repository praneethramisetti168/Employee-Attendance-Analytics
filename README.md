# Employee Attendance & Analytics API

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-v0.110+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MongoDB](https://img.shields.io/badge/MongoDB-7.0+-47A248.svg?logo=mongodb&logoColor=white)](https://www.mongodb.com/)
[![Uvicorn](https://img.shields.io/badge/ASGI-Uvicorn-2C5BB4.svg)](https://www.uvicorn.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An enterprise-grade Employee Attendance, Regularization, and Analytics REST API microservice built with **Python**, **FastAPI**, and **MongoDB 7.0**. Engineered for high throughput, atomic concurrency safety, and scalable analytical aggregations across 100,000+ attendance documents.

---

## Table of Contents
1. [System Architecture](#system-architecture)
2. [Key Capabilities](#key-capabilities)
3. [Business Rules Engine (R1 – R10)](#business-rules-engine-r1--r10)
4. [Concurrency & Technical Countermeasures](#concurrency--technical-countermeasures)
5. [API Specification & Endpoints](#api-specification--endpoints)
6. [Interactive Web Portal & Dashboard](#interactive-web-portal--dashboard)
7. [Installation & Execution Guide](#installation--execution-guide)
8. [Database Indexing & Explain Verification](#database-indexing--explain-verification)
9. [Part A: Starter Code Review Summary](#part-a-starter-code-review-summary)
10. [Part C: Architectural Decisions Summary](#part-c-architectural-decisions-summary)
11. [Repository Structure](#repository-structure)

---

## System Architecture

```
                       +---------------------------------------+
                       |          Client / Web Browser         |
                       +---------------------------------------+
                                           |
                                HTTP / JSON Requests
                                           v
                       +---------------------------------------+
                       |      FastAPI Application Server       |
                       |          (Uvicorn ASGI Engine)        |
                       +---------------------------------------+
                        |                 |                   |
                        v                 v                   v
              +------------------+ +-------------+ +--------------------+
              | Attendance &     | | Audit Trail | | MongoDB Analytics  |
              | Rule Engine      | | (Optimistic | | Aggregations       |
              | (R1 - R5, Half-Up| | Concurrency | | ($rank, $densify,  |
              |  Overnight Shifts) Control)      | |  $lookup pipelines)|
              +------------------+ +-------------+ +--------------------+
                                           |
                                  PyMongo BSON / TCP
                                           v
                       +---------------------------------------+
                       |           MongoDB 7.0 Database        |
                       |       Compound B-Tree Index Scans     |
                       |            (Zero COLLSCAN)            |
                       +---------------------------------------+
```

---

## Key Capabilities

* **Atomic Attendance Management**: Concurrency-safe punch-in and punch-out operations.
* **Overnight Shift Handling**: Accurately handles rotational shifts crossing midnight (e.g., `22:00` to `06:00`).
* **Optimistic Concurrency Control (OCC)**: Race-safe regularization endpoint (`PATCH /attendance/{emp_code}/{date}`) preserving audit history.
* **Complex MongoDB Aggregations**:
  * Cross-collection `$lookup` including zero-log employees in headcount.
  * Standard competition ranking with ties using `$setWindowFields` and `$rank`.
  * Continuous time-series gap filling using MongoDB `$densify` with 7-day rolling moving averages.
* **Sub-Millisecond Query Plans**: Backed by compound B-tree indexes; verified using MongoDB `executionStats` with zero `COLLSCAN`.
* **Integrated Web Portal**: Fully responsive single-page dashboard for punch events, employee directories, and live analytics.

---

## Business Rules Engine (R1 – R10)

The core domain model enforces ten business rules as defined in the contract:

| Rule | Title | Formal Specification | Implementation Strategy |
| :--- | :--- | :--- | :--- |
| **R1** | **Timezone & Attendance Date** | Shift times and dates are in IST (`UTC+05:30`). A record's `date` is the IST calendar date of its `punch_in`. For overnight shifts (`shift_end <= shift_start`), punches before `shift_end` belong to the previous day's shift. | `attendance_date()` evaluates shift boundaries in IST; converts all storage timestamps to UTC seconds. |
| **R2** | **Late Arrival & Grace Period** | 10-minute grace period. Punch-in is late only if strictly more than 10 minutes after `shift_start`. When late, `late_minutes = floor(elapsed since shift_start)`. | Integer floor division: `delta_s // 60` where `delta_s > 600`. `09:40:00` gives 0; `09:40:01` gives 10. |
| **R3** | **Overtime** | Whole minutes (floor) from `shift_end` to `punch_out`, counted only if $\ge 30$ minutes. Otherwise 0. | Advances target reference datetime by $+1$ day for overnight shifts. Returns `delta_s // 60` if $\ge 1800$s. |
| **R4** | **Work Hours Precision** | $(\text{punch\_out} - \text{punch\_in}) / 3600$, rounded Half-Up to 2 decimal places. | Python `decimal.Decimal` with `ROUND_HALF_UP` (bypasses Python's default banker's rounding). |
| **R5** | **Half Day** | After punch-out, `half_day = true` when rounded `work_hours < 4.50`. Counts as 0.5 present day. | Computed dynamically upon checkout or regularization; evaluated over rounded hours. |
| **R6** | **Presence Qualification** | `PRESENT`, `WFH`, and `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not. | Unified presence filter applied across all analytical pipelines. |
| **R7** | **Working Days & Joiners** | Monday through Friday, no holiday calendar. For mid-month joiners, working days count from `joined_on`. | Calendar date iteration counting `weekday < 5`. Weekend punches contribute to late/overtime but not present days. |
| **R8** | **Reported Numbers** | All floating point stats rounded to 2 decimals Half-Up; rates rounded to 4 decimals Half-Up. | Strict `Decimal.quantize()` quantization across all API serialization boundaries. |
| **R9** | **Headcount Completeness** | Counts all employees with `joined_on <= end of period`, even if they have **zero attendance logs**. | Aggregation starts from `employees` collection with `$lookup` and `$unwind(preserveNullAndEmptyArrays: true)`. |
| **R10**| **Pagination Standards** | `page` starts at 1; `page_size` default 20, max 100. `total` must accurately reflect applied filters. | Streamed cursor pagination via MongoDB `.skip().limit()`; `count_documents(q)` for exact totals. |

---

## Concurrency & Technical Countermeasures

### 1. Duplicate Punch-In Race Condition
* **Problem**: Concurrent punch-ins arriving at the same millisecond can both pass check-then-insert validations, causing duplicate records for the same employee on the same date.
* **Countermeasure**: Enforced a compound unique index in MongoDB on `(emp_code, date)` at startup. Writes are issued directly via `insert_one()`. When a parallel conflict occurs, the database storage engine rejects the collision with a `DuplicateKeyError`. The API intercepts this and returns `409 Conflict`.

### 2. Punch-Out Race Condition
* **Problem**: Concurrent punch-outs could overwrite each other or update an already-closed attendance record.
* **Countermeasure**: Executed atomically via:
  ```python
  db.attendance_logs.find_one_and_update(
      {"_id": rec["_id"], "punch_out": None},
      {"$set": {"punch_out": ts_out, "work_hours": wh, "overtime_minutes": ot, "half_day": half}},
      return_document=True
  )
  ```
  If another request closes the record first, the query matches zero rows and returns `None`, prompting the API to immediately return `409 Conflict`.

### 3. Concurrent Regularization (Optimistic Concurrency Control)
* **Problem**: Concurrent edits to an attendance record's history audit trail can lead to lost updates.
* **Countermeasure**: Verified using array length validation at the database query level:
  ```python
  {"_id": rec["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]}}
  ```
  If a race occurs, the length condition fails, rolling back the transaction and raising `409 Conflict`.

---

## API Specification & Endpoints

| HTTP Method | Path | Summary / Responsibility | Status Codes |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Liveness & readiness probe (verifies active MongoDB connection via ping). | `200`, `503` |
| `POST` | `/employees` | Registers an employee. Validates codes (`^EMP\d{4,6}$`) and ensures distinct shift bounds. | `201`, `409`, `422` |
| `GET` | `/employees` | Lists employees sorted by `emp_code ASC` with department filtering and pagination. | `200`, `422` |
| `POST` | `/attendance/punch-in` | Creates the day's record (R1, R2). Enforces `(emp_code, date)` uniqueness. | `201`, `404`, `409`, `422` |
| `POST` | `/attendance/punch-out` | Closes open punch records across midnight shifts (R3, R4, R5). | `200`, `404`, `409`, `422` |
| `GET` | `/attendance` | Paginated attendance log viewer with filtering by employee, dates, and status. | `200`, `422` |
| `PATCH`| `/attendance/{emp_code}/{date}` | Regularizes records, recomputes derived metrics, and appends audit trails. | `200`, `404`, `409`, `422` |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance summary (present days, leave days, attendance percentage). | `200`, `404`, `422` |
| `GET` | `/analytics/departments/summary` | Cross-collection department metrics (headcount with zero-log employees, average hours). | `200`, `422` |
| `GET` | `/analytics/leaderboard/late` | Late leaderboard using Standard Competition Ranking (`$rank` window function). | `200`, `422` |
| `GET` | `/analytics/departments/{department}/trend` | Gap-filled daily attendance series (`$densify`) with rolling 7-day moving average. | `200`, `404`, `422` |
| `GET` | `/admin/explain/{endpoint}` | Raw MongoDB `executionStats` query explain plan verifying index scan usage. | `200`, `422` |

---

## Interactive Web Portal & Dashboard

An integrated Web UI is served at `/` and `/ui`, providing:
* **Interactive Biometric Terminal**: Dropdown selection of registered employees with immediate Punch-In/Punch-Out feedback.
* **Real-Time Attendance Table**: Human-readable IST timestamps, color-coded status badges, and calculated hours.
* **Employee Management**: Directory view with an integrated modal form to create new employees.
* **Live Analytics Visualizer**: Month selector displaying aggregated department statistics and late-comers leaderboards with competition ranking medals (🥇, 🥈, 🥉).

To access the web interface:
* **Dashboard Portal**: [`http://localhost:8000/`](http://localhost:8000/)
* **Interactive Swagger UI**: [`http://localhost:8000/docs`](http://localhost:8000/docs)
* **OpenAPI Raw Specification**: [`http://localhost:8000/openapi.json`](http://localhost:8000/openapi.json)

---

## Installation & Execution Guide

### Prerequisites
* **Python 3.11+**
* **MongoDB 6.0+** (Local service, Docker container, or MongoDB Atlas cluster)

### 1. Clone the Repository
```bash
git clone https://github.com/praneethramisetti168/Employee-Attendance-Analytics.git
cd Employee-Attendance-Analytics
```

### 2. Configure Virtual Environment & Dependencies
```bash
# Create and activate virtual environment
python -m venv .venv

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1

# On Linux / macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration
Create a `.env` file in the project root (optional if using defaults):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 4. Seed Sample Dataset (Optional)
```bash
python sample_seed.py
```

### 5. Launch Application Server
```bash
python -m uvicorn app.main:app --port 8000 --reload
```
The application will initialize database indexes idempotently and start listening on port `8000`.

---

## Database Indexing & Explain Verification

The application ensures all analytical pipelines avoid collection scans (`COLLSCAN`) by declaring targeted compound B-Tree indexes at startup:

1. `employees.create_index([("emp_code", ASCENDING)], unique=True)`
2. `employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])`
3. `attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)`
4. `attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])`
5. `attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])`
6. `attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])`

You can verify query plans using the explain endpoint:
```bash
# Example: Explain attendance list query plan
curl "http://localhost:8000/admin/explain/attendance_list"

# Example: Explain employee monthly aggregation plan
curl "http://localhost:8000/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"
```

---

## Part A: Starter Code Review Summary

During initial review of the codebase, 16 issues were identified and resolved (documented in [`REVIEW.md`](REVIEW.md)):

* **Readiness Failure**: `GET /health` returned `200` without checking MongoDB connectivity. Fixed via `admin.command("ping")`.
* **Off-by-One Pagination**: `list_employees` skipped `page * page_size`, omitting page 1. Corrected to `(page - 1) * page_size`.
* **Unfiltered Totals**: `total` ignored the department filter. Fixed using `count_documents(q)`.
* **Check-Then-Insert Race Conditions**: Duplicate employees and punch-ins bypassed application checks. Resolved via database unique indexes catching `DuplicateKeyError`.
* **Timezone Inconsistencies**: Local naive timestamps converted to UTC-aware instants across all boundaries.
* **Overnight Overtime Failure**: Overtime calculation for overnight shifts evaluated against the wrong date. Corrected by advancing reference times by $+1$ day.
* **Memory Leaks**: `list_attendance` materialized the entire collection into memory. Converted to streaming database cursors.

---

## Part C: Architectural Decisions Summary

Key design decisions documented in [`DECISIONS.md`](DECISIONS.md):

1. **Index Strategy**: Enforces storage-level uniqueness on `emp_code` and `(emp_code, date)` while supporting default sorting on `(date DESC, emp_code ASC)`.
2. **Concurrency Handling**: Relies on storage-level atomic constraints and Optimistic Concurrency Control instead of distributed application locks.
3. **Competition Ranking**: Uses MongoDB `$rank` inside `$setWindowFields` to ensure tied scores share ranks and skip following ranks (1, 2, 2, 4), applying `limit` after ranking.
4. **Zero-Log Headcount**: Queries originate from the `employees` collection with `$lookup` and `$unwind(preserveNullAndEmptyArrays: true)`.
5. **Horizontal Scaling**: Recommends sharding `attendance_logs` on compound key `{ emp_code: 1, date: 1 }` to distribute write traffic evenly across shards without scatter-gather overhead.

---

## Repository Structure

```
.
├── app/
│   ├── __init__.py
│   └── main.py                 # Full FastAPI service, rule engine & dashboard UI
├── sample_data/
│   ├── attendance_logs.json    # Sample attendance test records
│   └── employees.json          # Sample employee directory records
├── .gitignore                  # Git ignore rules (prevents environment/cache leaks)
├── DATA_MODEL.md               # MongoDB schema & Extended JSON specifications
├── DECISIONS.md                # Technical decisions on indexing, concurrency & scaling
├── openapi.yaml                # OpenAPI 3.0.3 contract specification
├── PROBLEM_STATEMENT.docx      # HROne engineering assignment requirements
├── README.md                   # Project documentation & execution guide
├── REVIEW.md                   # Starter code audit & defect resolution table
├── sample_seed.py              # Extended JSON dataset loader
└── requirements.txt            # Python dependencies
```

---

## Author
* **Praneeth Ramisetti** — [GitHub Profile](https://github.com/praneethramisetti168)
* **Application**: Software Engineer Trainee — **HROne**
