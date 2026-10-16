"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars take priority)
"""
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

EPOCH_MS_MIN = 100_000_000_000   # ~2001-09-09, rejects second-level ints
EPOCH_MS_MAX = 4_102_444_800_000 # ~2100-01-01

# ---------------------------------------------------------------------------
# Index creation at startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    # employees – unique emp_code (enforces concurrent-safe uniqueness)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)

    # attendance_logs – primary compound key: unique per (emp_code, date)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    # Support filtering / sorting by date descending, then emp_code asc
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    # Support month-range queries on date + emp_code (for analytics)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)]
    )
    # Support department-scoped analytics efficiently
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)]
    )


# ---------------------------------------------------------------------------
# Helpers – rounding
# ---------------------------------------------------------------------------
def half_up(value: float, decimals: int) -> float:
    """Round a float to `decimals` places using ROUND_HALF_UP."""
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Helpers – time conversion
# ---------------------------------------------------------------------------
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds → UTC-aware datetime, truncated to whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """datetime → epoch milliseconds. Accepts both aware and naive (assumed UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def dt_to_ist(dt: datetime) -> datetime:
    """Convert a datetime to IST. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


# ---------------------------------------------------------------------------
# Helpers – business rules
# ---------------------------------------------------------------------------
def attendance_date(ts_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Return the attendance date string (YYYY-MM-DD, IST).
    For an overnight shift (shift_end <= shift_start), a punch-in whose IST
    time is earlier than shift_end belongs to the shift that started the
    previous calendar day.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        punch_minutes = ts_ist.hour * 60 + ts_ist.minute
        end_minutes = eh * 60 + em
        if punch_minutes < end_minutes:
            # belongs to the shift that started yesterday
            return (ts_ist - timedelta(days=1)).date().isoformat()
    return ts_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str) -> int:
    """
    R2: Late only if strictly more than 10:00 after shift_start.
    late_minutes = floor minutes since shift_start (not since grace end).
    """
    h, m = map(int, shift_start.split(":"))
    start = punch_in_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    delta_s = int((punch_in_ist - start).total_seconds())
    if delta_s <= 600:   # <= 10:00 → on time (grace period is inclusive of 10:00 exactly)
        return 0
    return delta_s // 60   # floor minutes from shift_start


def compute_overtime(punch_out_ist: datetime, shift_end: str, date_str: str, is_overnight: bool) -> int:
    """
    R3: Overtime = floor minutes from shift_end to punch_out, only if >= 30.
    For overnight shifts shift_end falls on the next calendar day.
    """
    h, m = map(int, shift_end.split(":"))
    base_date = datetime.fromisoformat(date_str)
    if is_overnight:
        base_date = base_date + timedelta(days=1)
    end_ist = base_date.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    delta_s = int((punch_out_ist - end_ist).total_seconds())
    if delta_s < 30 * 60:
        return 0
    return delta_s // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 dp."""
    secs = (punch_out - punch_in).total_seconds()
    return half_up(secs / 3600, 2)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh * 60 + em) <= (sh * 60 + sm)


# ---------------------------------------------------------------------------
# Helpers – serialisation
# ---------------------------------------------------------------------------
def _serialize_history_entry(entry: dict) -> dict:
    """Convert BSON dates in a history entry to epoch ms for the API response."""
    out = {}
    out["at"] = dt_to_ms(entry["at"])
    out["by"] = entry["by"]
    out["reason"] = entry["reason"]
    changes = {}
    for field, change in entry.get("changes", {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {
                "from": dt_to_ms(change["from"]) if change["from"] is not None else None,
                "to": dt_to_ms(change["to"]) if change["to"] is not None else None,
            }
        else:
            changes[field] = {"from": change["from"], "to": change["to"]}
    out["changes"] = changes
    return out


def _serialize_record(doc: dict) -> dict:
    """Serialize a MongoDB attendance_log doc to the API AttendanceRecord shape."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": dt_to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [_serialize_history_entry(e) for e in doc.get("history", [])],
    }


def _serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE  = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def val_emp_code(cls, v: str) -> str:
        if not EMP_CODE_RE.match(v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def val_email(cls, v: str) -> str:
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        if len(v) > 120:
            raise ValueError("email too long")
        return v

    @field_validator("name")
    @classmethod
    def val_name(cls, v: str) -> str:
        if not v or len(v) > 100:
            raise ValueError("name must be 1-100 chars")
        return v

    @field_validator("department")
    @classmethod
    def val_department(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("department must be 1-50 chars")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def val_shift_time(cls, v: str) -> str:
        if not TIME_RE.match(v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def val_joined_on(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on is not a valid date")
        return v

    @model_validator(mode="after")
    def val_shift_not_equal(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


def _validate_epoch_ms(v: Optional[int]) -> Optional[int]:
    """Raise ValueError if v is not a valid epoch-ms integer."""
    if v is None:
        return v
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("punched_at must be an integer epoch milliseconds value")
    if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
        raise ValueError(
            f"punched_at must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX} (milliseconds)"
        )
    return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_punched_at(cls, v):
        return _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def val_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH or ON_DUTY for punch-in")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_punched_at(cls, v):
        return _validate_epoch_ms(v)


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def val_epoch(cls, v):
        return _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def val_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("invalid status")
        return v

    @field_validator("reason")
    @classmethod
    def val_reason(cls, v: str) -> str:
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 chars")
        return v

    @field_validator("regularized_by")
    @classmethod
    def val_regularized_by(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 chars")
        return v


# ---------------------------------------------------------------------------
# Month / date helpers
# ---------------------------------------------------------------------------
def month_bounds(month: str):
    """Return (first_day_str, last_day_str) for a YYYY-MM string."""
    if not MONTH_RE.match(month):
        raise HTTPException(422, f"month must match YYYY-MM, got: {month}")
    year, mon = int(month[:4]), int(month[5:])
    first = datetime(year, mon, 1)
    if mon == 12:
        last = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = datetime(year, mon + 1, 1) - timedelta(days=1)
    return first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")


def count_working_days(from_str: str, to_str: str) -> int:
    """Count Mon–Fri days between two date strings (inclusive)."""
    start = datetime.strptime(from_str, "%Y-%m-%d")
    end = datetime.strptime(to_str, "%Y-%m-%d")
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Endpoints – System
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


# ---------------------------------------------------------------------------
# Endpoints – Employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return _serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [_serialize_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Endpoints – Attendance
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    ts_ist = dt_to_ist(ts)
    d = attendance_date(ts_ist, emp["shift_start"], emp["shift_end"])

    late = compute_late_minutes(ts_ist, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts.replace(microsecond=0),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    doc.pop("_id", None)
    return _serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts_out = ms_to_dt(body.punched_at)
    else:
        ts_out = datetime.now(tz=timezone.utc).replace(microsecond=0)
    ts_out = ts_out.replace(microsecond=0)

    # Find the most recent open record where punch_in <= ts_out
    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts_out},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        # Check if there's a record that's already punched out (for 409)
        closed = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": ts_out},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if closed:
            raise HTTPException(409, "already punched out for this record")
        raise HTTPException(404, "no open punch-in found for this employee")

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # 422: punch_out <= punch_in or more than 24 hours after punch_in
    delta = (ts_out - punch_in_dt).total_seconds()
    if delta <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if delta > 86400:
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    wh = compute_work_hours(punch_in_dt, ts_out)
    half = wh < 4.50

    overnight = is_overnight_shift(emp["shift_start"], emp["shift_end"])
    ot = compute_overtime(dt_to_ist(ts_out), emp["shift_end"], rec["date"], overnight)

    # Race-safe: only update the record if punch_out is still None
    result = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts_out,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": half,
            }
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "already punched out for this record")

    return _serialize_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and not DATE_RE.match(date_from):
        raise HTTPException(422, "date_from must be YYYY-MM-DD")
    if date_to and not DATE_RE.match(date_to):
        raise HTTPException(422, "date_to must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [_serialize_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # Validate date format
    if not DATE_RE.match(date):
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no attendance record found for this date")

    # --- Build the new state ---
    new_status = body.status if body.status is not None else rec["status"]
    new_punch_in = ms_to_dt(body.punch_in).replace(microsecond=0) if body.punch_in is not None else rec.get("punch_in")
    new_punch_out = ms_to_dt(body.punch_out).replace(microsecond=0) if body.punch_out is not None else rec.get("punch_out")

    # Validation: ABSENT/LEAVE cannot have punch times in the request
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE records cannot have punch times")
        new_punch_in = None
        new_punch_out = None

    # Presence status requires a punch_in
    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        raise HTTPException(422, "presence status requires a punch_in")

    # punch_in must remain on the record's attendance date (R1)
    if new_punch_in is not None:
        pin_ist = dt_to_ist(new_punch_in)
        actual_date = attendance_date(pin_ist, emp["shift_start"], emp["shift_end"])
        if actual_date != date:
            raise HTTPException(422, "punch_in does not fall on the record's attendance date")

    # punch_out must be after punch_in and within 24 hours
    if new_punch_out is not None and new_punch_in is not None:
        pin_utc = new_punch_in if new_punch_in.tzinfo else new_punch_in.replace(tzinfo=timezone.utc)
        pout_utc = new_punch_out if new_punch_out.tzinfo else new_punch_out.replace(tzinfo=timezone.utc)
        delta = (pout_utc - pin_utc).total_seconds()
        if delta <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")
        if delta > 86400:
            raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    # --- Recompute derived fields ---
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_wh = None
        new_ot = 0
        new_half = False
    elif new_punch_in is not None:
        pin_ist = dt_to_ist(new_punch_in)
        new_late = compute_late_minutes(pin_ist, emp["shift_start"])
        if new_punch_out is not None:
            pout_ist = dt_to_ist(new_punch_out)
            pin_for_wh = new_punch_in if new_punch_in.tzinfo else new_punch_in.replace(tzinfo=timezone.utc)
            pout_for_wh = new_punch_out if new_punch_out.tzinfo else new_punch_out.replace(tzinfo=timezone.utc)
            new_wh = compute_work_hours(pin_for_wh, pout_for_wh)
            new_half = new_wh < 4.50
            overnight = is_overnight_shift(emp["shift_start"], emp["shift_end"])
            new_ot = compute_overtime(pout_ist, emp["shift_end"], date, overnight)
        else:
            new_wh = None
            new_ot = 0
            new_half = False
    else:
        new_late = rec.get("late_minutes", 0)
        new_wh = rec.get("work_hours")
        new_ot = rec.get("overtime_minutes", 0)
        new_half = rec.get("half_day", False)

    # --- Build the changes dict (only fields that actually changed) ---
    old_pin = rec.get("punch_in")
    old_pout = rec.get("punch_out")

    # Normalise stored datetimes to UTC-aware for comparison
    def _aw(dt):
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    changes: dict = {}
    if new_status != rec["status"]:
        changes["status"] = {"from": rec["status"], "to": new_status}

    _old_pin = _aw(old_pin)
    _new_pin = _aw(new_punch_in)
    if _old_pin != _new_pin:
        changes["punch_in"] = {
            "from": dt_to_ms(_old_pin) if _old_pin else None,
            "to": dt_to_ms(_new_pin) if _new_pin else None,
        }

    _old_pout = _aw(old_pout)
    _new_pout = _aw(new_punch_out)
    if _old_pout != _new_pout:
        changes["punch_out"] = {
            "from": dt_to_ms(_old_pout) if _old_pout else None,
            "to": dt_to_ms(_new_pout) if _new_pout else None,
        }

    if new_late != rec.get("late_minutes", 0):
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late}
    if new_wh != rec.get("work_hours"):
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_wh}
    if new_ot != rec.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": new_ot}
    if new_half != rec.get("half_day", False):
        changes["half_day"] = {"from": rec.get("half_day", False), "to": new_half}

    if not changes:
        raise HTTPException(422, "request changes nothing")

    # --- Build history entry (store datetimes as BSON dates for punch times) ---
    history_changes: dict = {}
    for field, ch in changes.items():
        if field in ("punch_in", "punch_out"):
            history_changes[field] = {
                "from": ms_to_dt(ch["from"]) if ch["from"] is not None else None,
                "to": ms_to_dt(ch["to"]) if ch["to"] is not None else None,
            }
        else:
            history_changes[field] = ch

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": history_changes,
    }

    update_fields = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_half,
    }

    # Race-safe: check history length to detect concurrent edits
    current_history_len = len(rec.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "concurrent edit detected; please retry")

    return _serialize_record(result)


# ---------------------------------------------------------------------------
# Analytics – Employee Monthly
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    if not MONTH_RE.match(month):
        raise HTTPException(422, "month must be YYYY-MM")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    first_day, last_day = month_bounds(month)
    # working days from max(joined_on, first_day) to last_day
    wd_from = max(first_day, emp["joined_on"])
    working_days = count_working_days(wd_from, last_day) if wd_from <= last_day else 0

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }
        },
        {
            "$addFields": {
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],  # Mon=2, Tue=3, Wed=4, Thu=5, Fri=6 in MongoDB
                    ]
                },
                "is_present": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present"]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days = r["present_days"]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = (
        half_up(present_days / working_days * 100, 2) if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# Analytics – Department Summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    if not MONTH_RE.match(month):
        raise HTTPException(422, "month must be YYYY-MM")
    first_day, last_day = month_bounds(month)

    # Step 1: headcount per department (employees joined on or before last_day)
    # We use a $lookup from employees into attendance_logs scoped to the month.
    dept_filter: dict = {}
    if department:
        dept_filter["department"] = department

    # Main aggregation: start from employees, $lookup attendance_logs for the month
    pipeline = [
        # Filter employees who have joined on or before last_day
        {
            "$match": {
                **dept_filter,
                "joined_on": {"$lte": last_day},
            }
        },
        # Lookup their attendance logs in the month
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ec"]},
                            "date": {"$gte": first_day, "$lte": last_day},
                        }
                    }
                ],
                "as": "logs",
            }
        },
        # Unwind logs (preserveNullAndEmptyArrays to keep employees with no logs)
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        # Add computed fields per log
        {
            "$addFields": {
                "log_is_weekday": {
                    "$cond": {
                        "if": {"$ifNull": ["$logs", False]},
                        "then": {
                            "$in": [
                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                [2, 3, 4, 5, 6],
                            ]
                        },
                        "else": False,
                    }
                },
                "log_is_present": {
                    "$cond": {
                        "if": {"$ifNull": ["$logs", False]},
                        "then": {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        "else": False,
                    }
                },
            }
        },
        # Group by department and employee to count the employee once in headcount
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "department": {"$first": "$department"},
                "present_days_contrib": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$log_is_weekday", "$log_is_present"]},
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "wh_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    "$log_is_present",
                                    {"$ifNull": ["$logs.work_hours", False]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        # Now group by department to aggregate over all employees
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days_contrib"},
                "all_wh": {"$push": "$wh_values"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        # Flatten all_wh (array of arrays) and compute avg
        {
            "$addFields": {
                "flat_wh": {
                    "$reduce": {
                        "input": "$all_wh",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                }
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$flat_wh"}, 0]},
                        {"$avg": "$flat_wh"},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        avg_wh = half_up(r["avg_work_hours"], 2) if r["avg_work_hours"] is not None else None
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Analytics – Late Leaderboard
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    if not MONTH_RE.match(month):
        raise HTTPException(422, "month must be YYYY-MM")
    first_day, last_day = month_bounds(month)

    pipeline = [
        # Match logs in the month that have late_minutes > 0
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0},
            }
        },
        # Group by emp_code
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Join to employees for name / department
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        # Only keep records where the employee exists
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
    ]

    # Filter by department if given
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        # Sort for ranking: total desc, emp_code asc
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        # Compute standard competition rank using $setWindowFields
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {},
                    }
                },
            }
        },
        # Apply limit AFTER ranking (include all ties at rank <= limit)
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": rows}


# ---------------------------------------------------------------------------
# Analytics – Department Trend
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    if not DATE_RE.match(from_) or not DATE_RE.match(to):
        raise HTTPException(422, "from and to must be YYYY-MM-DD")
    if to < from_:
        raise HTTPException(422, "to must be >= from")

    from_dt = datetime.strptime(from_, "%Y-%m-%d")
    to_dt = datetime.strptime(to, "%Y-%m-%d")
    if (to_dt - from_dt).days > 91:
        raise HTTPException(422, "date range exceeds 92 days")

    # Check the department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    # Build a daily series pipeline using $densify
    pipeline = [
        # Start from attendance_logs for this department's employees in range
        {
            "$match": {
                "date": {"$gte": from_, "$lte": to},
            }
        },
        # Join to employees to filter by department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        # Annotate is_present, is_half_day
        {
            "$addFields": {
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "present_contrib": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                        0,
                    ]
                },
                "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
            }
        },
        # Group by date
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": "$present_contrib"},
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
            }
        },
        # Project to a BSON date field for $densify (densify needs Date type, not string)
        {
            "$project": {
                "_id": 0,
                "date_bson": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Densify to fill gaps — bounds must be BSON Date objects (passed as Python datetimes)
        {
            "$densify": {
                "field": "date_bson",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.strptime(from_, "%Y-%m-%d").replace(tzinfo=timezone.utc),
                        # upper bound is exclusive in $densify, so add one day
                        datetime.strptime(to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1),
                    ],
                },
            }
        },
        # Restore missing values after densify and convert date_bson back to string
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_bson"}},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_bson"},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        # Guard: keep only rows in [from_, to] (densify upper bound is exclusive, but belt-and-suspenders)
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$sort": {"date_bson": ASCENDING}},
    ]

    # We need headcount per day (employees with joined_on <= that day in department).
    # This is complex to do purely in aggregation with per-day headcount varying.
    # We'll compute headcount in a simpler way: fetch all dept employees with joined_on <= to,
    # then for each day, count those with joined_on <= date.

    dept_employees = list(
        db.employees.find(
            {"department": department, "joined_on": {"$lte": to}},
            {"emp_code": 1, "joined_on": 1, "_id": 0},
        ).sort("joined_on", ASCENDING)
    )

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Build result with headcount and attendance_rate per day, then compute moving avg
    result_items = []
    for row in rows:
        date_str = row["date"] if isinstance(row["date"], str) else row["date"].strftime("%Y-%m-%d")
        hc = sum(1 for e in dept_employees if e["joined_on"] <= date_str)

        is_working = row["is_working_day"]
        present_count = row["present_count"]
        late_count = row["late_count"]

        if is_working and hc > 0:
            att_rate = half_up(present_count / hc, 4)
        else:
            att_rate = None

        result_items.append({
            "date": date_str,
            "is_working_day": is_working,
            "headcount": hc,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": att_rate,
            "moving_avg_7d": None,  # filled next
        })

    # Compute 7-day moving average over non-null attendance_rate values
    for i, item in enumerate(result_items):
        window = [
            result_items[j]["attendance_rate"]
            for j in range(max(0, i - 6), i + 1)
            if result_items[j]["attendance_rate"] is not None
        ]
        item["moving_avg_7d"] = half_up(sum(window) / len(window), 4) if window else None

    return {"department": department, "items": result_items}


# ---------------------------------------------------------------------------
# Admin – Explain
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"endpoint must be one of {sorted(valid_endpoints)}")

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        exp = db.command("explain", find_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "month is required and must be YYYY-MM")
        first_day, last_day = month_bounds(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first_day, "$lte": last_day},
                }
            },
            {
                "$addFields": {
                    "is_weekday": {
                        "$in": [
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                            [2, 3, 4, 5, 6],
                        ]
                    },
                    "is_present": {
                        "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_weekday", "$is_present"]},
                                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_summary":
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "month is required and must be YYYY-MM")
        first_day, last_day = month_bounds(month)
        dept_filter: dict = {}
        if department:
            dept_filter["department"] = department
        pipeline = [
            {"$match": {**dept_filter, "joined_on": {"$lte": last_day}}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"ec": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {"$eq": ["$emp_code", "$$ec"]},
                                "date": {"$gte": first_day, "$lte": last_day},
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True,
                }
            },
        ]
        agg_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}

    elif endpoint == "late_leaderboard":
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "month is required and must be YYYY-MM")
        first_day, last_day = month_bounds(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first_day, "$lte": last_day},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$match": {"emp": {"$ne": []}}},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline += [
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {
                            "$rank": {},
                        }
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
        ]
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_ or not to:
            raise HTTPException(422, "from and to are required for department_trend")
        if not DATE_RE.match(from_) or not DATE_RE.match(to):
            raise HTTPException(422, "from and to must be YYYY-MM-DD")
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": from_, "$lte": to},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
        ]
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        exp = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}


# ---------------------------------------------------------------------------
# Interactive Web Application UI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HROne - Employee Attendance & Analytics Portal</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .tab-btn.active { border-bottom: 2px solid #3b82f6; color: #1d4ed8; font-weight: 600; }
    </style>
</head>
<body class="bg-slate-50 text-slate-800 font-sans min-h-screen">
    <!-- Navbar -->
    <header class="bg-white border-b border-slate-200 sticky top-0 z-50">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
            <div class="flex items-center space-x-3">
                <div class="w-10 h-10 rounded-lg bg-blue-600 flex items-center justify-center text-white font-bold text-xl shadow-md">
                    H1
                </div>
                <div>
                    <h1 class="text-lg font-bold text-slate-900 leading-tight">HROne Attendance & Analytics</h1>
                    <p class="text-xs text-slate-500">FastAPI & MongoDB Microservice</p>
                </div>
            </div>
            <div class="flex items-center space-x-4">
                <div id="system-status" class="flex items-center space-x-2 px-3 py-1 bg-green-50 text-green-700 border border-green-200 rounded-full text-xs font-medium">
                    <span class="w-2 h-2 rounded-full bg-green-500 animate-pulse"></span>
                    <span>API Online</span>
                </div>
                <a href="/docs" target="_blank" class="px-3.5 py-1.5 bg-slate-900 hover:bg-slate-800 text-white rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition">
                    <i class="fa-solid fa-code"></i>
                    <span>OpenAPI Docs (/docs)</span>
                </a>
            </div>
        </div>
    </header>

    <!-- Main Container -->
    <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">
        
        <!-- Alerts Banner -->
        <div id="alert-banner" class="hidden p-4 rounded-xl border text-sm font-medium transition duration-300"></div>

        <!-- Punch & Action Bar -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <!-- Punch Terminal -->
            <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm space-y-4">
                <div class="flex items-center justify-between">
                    <h2 class="text-base font-bold text-slate-900 flex items-center gap-2">
                        <i class="fa-solid fa-fingerprint text-blue-600"></i> Attendance Terminal
                    </h2>
                    <span class="text-xs bg-blue-50 text-blue-700 px-2 py-0.5 rounded font-mono" id="current-clock">--:--:--</span>
                </div>
                
                <div class="space-y-3">
                    <div>
                        <label class="block text-xs font-semibold text-slate-600 mb-1">Select Employee</label>
                        <select id="punch-emp-select" class="w-full text-sm border border-slate-300 rounded-lg p-2.5 bg-slate-50 focus:bg-white focus:ring-2 focus:ring-blue-500 outline-none">
                            <option value="">Loading employees...</option>
                        </select>
                    </div>

                    <div>
                        <label class="block text-xs font-semibold text-slate-600 mb-1">Work Mode</label>
                        <select id="punch-status" class="w-full text-sm border border-slate-300 rounded-lg p-2.5 bg-slate-50 focus:bg-white focus:ring-2 focus:ring-blue-500 outline-none">
                            <option value="PRESENT">PRESENT (Office)</option>
                            <option value="WFH">WFH (Work From Home)</option>
                            <option value="ON_DUTY">ON_DUTY (Field / Client)</option>
                        </select>
                    </div>

                    <div class="grid grid-cols-2 gap-3 pt-2">
                        <button onclick="handlePunchIn()" class="w-full py-2.5 px-4 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg text-sm font-bold shadow-sm transition flex items-center justify-center gap-2">
                            <i class="fa-solid fa-arrow-right-to-bracket"></i> Punch In
                        </button>
                        <button onclick="handlePunchOut()" class="w-full py-2.5 px-4 bg-amber-600 hover:bg-amber-700 text-white rounded-lg text-sm font-bold shadow-sm transition flex items-center justify-center gap-2">
                            <i class="fa-solid fa-arrow-right-from-bracket"></i> Punch Out
                        </button>
                    </div>
                </div>
            </div>

            <!-- Quick Stats Overview -->
            <div class="lg:col-span-2 bg-gradient-to-br from-blue-900 to-indigo-900 text-white p-6 rounded-2xl shadow-sm flex flex-col justify-between">
                <div>
                    <div class="flex items-center justify-between">
                        <span class="text-xs uppercase tracking-wider font-semibold text-blue-200">System Information</span>
                        <span class="px-2 py-0.5 rounded bg-blue-800 text-blue-200 text-xs">MongoDB 7.0 Connected</span>
                    </div>
                    <h3 class="text-2xl font-black mt-2 text-white">HR Attendance Engine v2.0</h3>
                    <p class="text-sm text-blue-200 mt-1">
                        Handles business rules R1–R10: grace periods, overnight shifts, competition ranking, and gap-filling analytics.
                    </p>
                </div>
                <div class="grid grid-cols-3 gap-4 pt-4 border-t border-blue-800/60 mt-4 text-center">
                    <div>
                        <div class="text-2xl font-extrabold" id="stat-total-emp">--</div>
                        <div class="text-xs text-blue-300">Total Employees</div>
                    </div>
                    <div>
                        <div class="text-2xl font-extrabold" id="stat-total-logs">--</div>
                        <div class="text-xs text-blue-300">Attendance Logs</div>
                    </div>
                    <div>
                        <div class="text-2xl font-extrabold" id="stat-total-dept">--</div>
                        <div class="text-xs text-blue-300">Departments</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Navigation Tabs -->
        <div class="border-b border-slate-200 flex space-x-8 text-sm">
            <button onclick="switchTab('tab-attendance')" id="tab-btn-attendance" class="tab-btn active pb-3 flex items-center gap-2 transition">
                <i class="fa-solid fa-list-check"></i> Attendance Logs
            </button>
            <button onclick="switchTab('tab-employees')" id="tab-btn-employees" class="tab-btn pb-3 flex items-center gap-2 text-slate-500 hover:text-slate-800 transition">
                <i class="fa-solid fa-users"></i> Employees Directory
            </button>
            <button onclick="switchTab('tab-analytics')" id="tab-btn-analytics" class="tab-btn pb-3 flex items-center gap-2 text-slate-500 hover:text-slate-800 transition">
                <i class="fa-solid fa-chart-pie"></i> Analytics & Leaderboard
            </button>
        </div>

        <!-- TAB 1: Attendance Logs -->
        <div id="tab-attendance" class="space-y-4">
            <div class="flex items-center justify-between">
                <h3 class="text-base font-bold text-slate-900">Recent Attendance Records</h3>
                <button onclick="loadAttendance()" class="px-3 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition">
                    <i class="fa-solid fa-rotate-right"></i> Refresh
                </button>
            </div>
            <div class="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm">
                        <thead class="bg-slate-50 text-slate-600 text-xs uppercase font-bold border-b border-slate-200">
                            <tr>
                                <th class="py-3 px-4">Date</th>
                                <th class="py-3 px-4">Employee</th>
                                <th class="py-3 px-4">Status</th>
                                <th class="py-3 px-4">Punch In</th>
                                <th class="py-3 px-4">Punch Out</th>
                                <th class="py-3 px-4">Work Hours</th>
                                <th class="py-3 px-4">Late (mins)</th>
                                <th class="py-3 px-4">Overtime</th>
                                <th class="py-3 px-4">Half Day</th>
                            </tr>
                        </thead>
                        <tbody id="attendance-tbody" class="divide-y divide-slate-100">
                            <tr><td colspan="9" class="p-6 text-center text-slate-400">Loading attendance records...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- TAB 2: Employees Directory -->
        <div id="tab-employees" class="hidden space-y-6">
            <div class="flex items-center justify-between">
                <h3 class="text-base font-bold text-slate-900">Registered Employees</h3>
                <button onclick="document.getElementById('new-emp-modal').classList.remove('hidden')" class="px-3.5 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-xs font-bold flex items-center gap-1.5 shadow-sm transition">
                    <i class="fa-solid fa-user-plus"></i> Add New Employee
                </button>
            </div>
            <div class="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm">
                        <thead class="bg-slate-50 text-slate-600 text-xs uppercase font-bold border-b border-slate-200">
                            <tr>
                                <th class="py-3 px-4">Code</th>
                                <th class="py-3 px-4">Name</th>
                                <th class="py-3 px-4">Email</th>
                                <th class="py-3 px-4">Department</th>
                                <th class="py-3 px-4">Shift (IST)</th>
                                <th class="py-3 px-4">Joined On</th>
                            </tr>
                        </thead>
                        <tbody id="employees-tbody" class="divide-y divide-slate-100">
                            <tr><td colspan="6" class="p-6 text-center text-slate-400">Loading employees...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- TAB 3: Analytics & Leaderboard -->
        <div id="tab-analytics" class="hidden space-y-8">
            <div class="flex flex-wrap items-center justify-between gap-4 bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
                <div class="flex items-center gap-3">
                    <span class="text-xs font-bold text-slate-700 uppercase tracking-wider">Month:</span>
                    <input type="month" id="analytics-month" value="2026-07" class="border border-slate-300 rounded-lg p-2 text-sm bg-slate-50 outline-none focus:ring-2 focus:ring-blue-500">
                    <button onclick="loadAnalytics()" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-xs font-bold transition flex items-center gap-1.5 shadow-sm">
                        <i class="fa-solid fa-chart-line"></i> Calculate Analytics
                    </button>
                </div>
                <div class="text-xs text-slate-500">
                    Aggregated live via MongoDB aggregation pipelines
                </div>
            </div>

            <!-- Department Cards -->
            <div class="space-y-3">
                <h4 class="text-sm font-bold text-slate-800 uppercase tracking-wider">Department Attendance Summary</h4>
                <div id="dept-summary-cards" class="grid grid-cols-1 md:grid-cols-3 gap-6">
                    <div class="col-span-full p-6 text-center text-slate-400 bg-white rounded-xl border border-slate-200">
                        Click "Calculate Analytics" to load department metrics.
                    </div>
                </div>
            </div>

            <!-- Late Leaderboard -->
            <div class="space-y-3">
                <h4 class="text-sm font-bold text-slate-800 uppercase tracking-wider">Late-Comers Leaderboard (Ranked with Ties)</h4>
                <div class="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm">
                            <thead class="bg-slate-50 text-slate-600 text-xs uppercase font-bold border-b border-slate-200">
                                <tr>
                                    <th class="py-3 px-4">Rank</th>
                                    <th class="py-3 px-4">Employee</th>
                                    <th class="py-3 px-4">Department</th>
                                    <th class="py-3 px-4">Total Late (Minutes)</th>
                                    <th class="py-3 px-4">Late Occurrences</th>
                                </tr>
                            </thead>
                            <tbody id="leaderboard-tbody" class="divide-y divide-slate-100">
                                <tr><td colspan="5" class="p-6 text-center text-slate-400">Click "Calculate Analytics" to view leaderboard.</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>

    </main>

    <!-- Modal: Add New Employee -->
    <div id="new-emp-modal" class="hidden fixed inset-0 bg-slate-900/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
        <div class="bg-white rounded-2xl max-w-md w-full p-6 shadow-xl space-y-4">
            <div class="flex items-center justify-between border-b pb-3">
                <h3 class="text-base font-bold text-slate-900">Register New Employee</h3>
                <button onclick="document.getElementById('new-emp-modal').classList.add('hidden')" class="text-slate-400 hover:text-slate-600">
                    <i class="fa-solid fa-xmark text-lg"></i>
                </button>
            </div>
            <form onsubmit="handleCreateEmployee(event)" class="space-y-3 text-sm">
                <div>
                    <label class="block text-xs font-semibold text-slate-600 mb-1">Employee Code</label>
                    <input type="text" id="new-emp-code" required placeholder="EMP0007" pattern="^EMP\d{4,6}$" class="w-full border rounded-lg p-2.5 outline-none focus:ring-2 focus:ring-blue-500">
                    <span class="text-[10px] text-slate-400">Format: EMP followed by 4-6 digits</span>
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-600 mb-1">Full Name</label>
                    <input type="text" id="new-emp-name" required placeholder="Rahul Sharma" class="w-full border rounded-lg p-2.5 outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-600 mb-1">Email</label>
                    <input type="email" id="new-emp-email" required placeholder="rahul@example.com" class="w-full border rounded-lg p-2.5 outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-600 mb-1">Department</label>
                    <input type="text" id="new-emp-dept" required placeholder="Engineering" class="w-full border rounded-lg p-2.5 outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-xs font-semibold text-slate-600 mb-1">Shift Start</label>
                        <input type="text" id="new-emp-start" value="09:30" pattern="^([01]\d|2[0-3]):[0-5]\d$" class="w-full border rounded-lg p-2.5 outline-none focus:ring-2 focus:ring-blue-500">
                    </div>
                    <div>
                        <label class="block text-xs font-semibold text-slate-600 mb-1">Shift End</label>
                        <input type="text" id="new-emp-end" value="18:30" pattern="^([01]\d|2[0-3]):[0-5]\d$" class="w-full border rounded-lg p-2.5 outline-none focus:ring-2 focus:ring-blue-500">
                    </div>
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-600 mb-1">Joined On</label>
                    <input type="date" id="new-emp-joined" required value="2026-07-01" class="w-full border rounded-lg p-2.5 outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                <div class="pt-3 flex gap-3">
                    <button type="button" onclick="document.getElementById('new-emp-modal').classList.add('hidden')" class="w-1/2 py-2.5 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-lg font-semibold transition">Cancel</button>
                    <button type="submit" class="w-1/2 py-2.5 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-semibold shadow-sm transition">Create Employee</button>
                </div>
            </form>
        </div>
    </div>

    <!-- JavaScript Logic -->
    <script>
        // Clock
        setInterval(() => {
            const now = new Date();
            document.getElementById('current-clock').textContent = now.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' });
        }, 1000);

        function showAlert(msg, isSuccess = true) {
            const b = document.getElementById('alert-banner');
            b.className = `p-4 rounded-xl border text-sm font-medium ${isSuccess ? 'bg-emerald-50 text-emerald-800 border-emerald-200' : 'bg-rose-50 text-rose-800 border-rose-200'}`;
            b.innerHTML = `<div class="flex items-center gap-2"><i class="fa-solid ${isSuccess ? 'fa-circle-check text-emerald-600' : 'fa-circle-exclamation text-rose-600'}"></i> ${msg}</div>`;
            b.classList.remove('hidden');
            setTimeout(() => b.classList.add('hidden'), 6000);
        }

        function switchTab(tabId) {
            ['tab-attendance', 'tab-employees', 'tab-analytics'].forEach(id => {
                document.getElementById(id).classList.add('hidden');
                document.getElementById('tab-btn-' + id.replace('tab-', '')).classList.remove('active', 'text-blue-700');
                document.getElementById('tab-btn-' + id.replace('tab-', '')).classList.add('text-slate-500');
            });
            document.getElementById(tabId).classList.remove('hidden');
            const activeBtn = document.getElementById('tab-btn-' + tabId.replace('tab-', ''));
            activeBtn.classList.add('active', 'text-blue-700');
            activeBtn.classList.remove('text-slate-500');
        }

        // Format Epoch Milliseconds to IST string
        function formatEpoch(ms) {
            if (!ms) return '<span class="text-slate-300">-</span>';
            const d = new Date(ms);
            return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' });
        }

        // Load Employees
        async function loadEmployees() {
            try {
                const res = await fetch('/employees?page=1&page_size=100');
                const data = await res.json();
                const sel = document.getElementById('punch-emp-select');
                sel.innerHTML = '<option value="">-- Choose an employee --</option>';
                const tbody = document.getElementById('employees-tbody');
                tbody.innerHTML = '';
                
                const depts = new Set();
                data.items.forEach(e => {
                    depts.add(e.department);
                    sel.innerHTML += `<option value="${e.emp_code}">${e.emp_code} - ${e.name} (${e.department})</option>`;
                    tbody.innerHTML += `
                        <tr class="hover:bg-slate-50/80 transition">
                            <td class="py-3 px-4 font-mono font-bold text-blue-600">${e.emp_code}</td>
                            <td class="py-3 px-4 font-semibold text-slate-900">${e.name}</td>
                            <td class="py-3 px-4 text-slate-500">${e.email}</td>
                            <td class="py-3 px-4"><span class="px-2 py-0.5 rounded-full bg-slate-100 text-slate-700 text-xs font-medium">${e.department}</span></td>
                            <td class="py-3 px-4 font-mono text-xs">${e.shift_start} - ${e.shift_end}</td>
                            <td class="py-3 px-4 text-slate-500 font-mono text-xs">${e.joined_on}</td>
                        </tr>
                    `;
                });
                document.getElementById('stat-total-emp').textContent = data.total;
                document.getElementById('stat-total-dept').textContent = depts.size;
            } catch (err) {
                console.error(err);
            }
        }

        // Load Attendance
        async function loadAttendance() {
            try {
                const res = await fetch('/attendance?page=1&page_size=20');
                const data = await res.json();
                const tbody = document.getElementById('attendance-tbody');
                tbody.innerHTML = '';
                document.getElementById('stat-total-logs').textContent = data.total;

                if (data.items.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="9" class="p-6 text-center text-slate-400">No attendance logs found.</td></tr>';
                    return;
                }

                data.items.forEach(r => {
                    let statusPill = '';
                    if (r.status === 'PRESENT') statusPill = '<span class="px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-800 text-xs font-bold">PRESENT</span>';
                    else if (r.status === 'WFH') statusPill = '<span class="px-2 py-0.5 rounded-full bg-blue-100 text-blue-800 text-xs font-bold">WFH</span>';
                    else if (r.status === 'ON_DUTY') statusPill = '<span class="px-2 py-0.5 rounded-full bg-purple-100 text-purple-800 text-xs font-bold">ON_DUTY</span>';
                    else if (r.status === 'LEAVE') statusPill = '<span class="px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 text-xs font-bold">LEAVE</span>';
                    else statusPill = '<span class="px-2 py-0.5 rounded-full bg-rose-100 text-rose-800 text-xs font-bold">ABSENT</span>';

                    tbody.innerHTML += `
                        <tr class="hover:bg-slate-50/80 transition">
                            <td class="py-3 px-4 font-mono font-medium">${r.date}</td>
                            <td class="py-3 px-4 font-mono font-bold text-slate-900">${r.emp_code}</td>
                            <td class="py-3 px-4">${statusPill}</td>
                            <td class="py-3 px-4 font-mono text-xs">${formatEpoch(r.punch_in)}</td>
                            <td class="py-3 px-4 font-mono text-xs">${formatEpoch(r.punch_out)}</td>
                            <td class="py-3 px-4 font-bold text-slate-800">${r.work_hours !== null ? r.work_hours + ' hrs' : '<span class="text-slate-300">-</span>'}</td>
                            <td class="py-3 px-4 font-medium ${r.late_minutes > 0 ? 'text-rose-600 font-bold' : 'text-slate-400'}">${r.late_minutes}</td>
                            <td class="py-3 px-4 font-medium ${r.overtime_minutes > 0 ? 'text-emerald-600 font-bold' : 'text-slate-400'}">${r.overtime_minutes}m</td>
                            <td class="py-3 px-4">${r.half_day ? '<span class="text-amber-600 font-bold">Yes (0.5d)</span>' : '<span class="text-slate-400">No</span>'}</td>
                        </tr>
                    `;
                });
            } catch (err) {
                console.error(err);
            }
        }

        // Punch In Action
        async function handlePunchIn() {
            const emp_code = document.getElementById('punch-emp-select').value;
            const status = document.getElementById('punch-status').value;
            if (!emp_code) { alert('Please select an employee'); return; }

            try {
                const res = await fetch('/attendance/punch-in', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ emp_code, status })
                });
                const data = await res.json();
                if (res.status === 201) {
                    showAlert(`Successfully punched in for ${emp_code} on ${data.date}. Late minutes: ${data.late_minutes} min.`);
                    loadAttendance();
                } else {
                    showAlert(data.detail || 'Punch in failed', false);
                }
            } catch (err) {
                showAlert('Error submitting punch-in', false);
            }
        }

        // Punch Out Action
        async function handlePunchOut() {
            const emp_code = document.getElementById('punch-emp-select').value;
            if (!emp_code) { alert('Please select an employee'); return; }

            try {
                const res = await fetch('/attendance/punch-out', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ emp_code })
                });
                const data = await res.json();
                if (res.status === 200) {
                    showAlert(`Successfully punched out for ${emp_code}! Work Hours: ${data.work_hours} hrs, Overtime: ${data.overtime_minutes} min.`);
                    loadAttendance();
                } else {
                    showAlert(data.detail || 'Punch out failed', false);
                }
            } catch (err) {
                showAlert('Error submitting punch-out', false);
            }
        }

        // Create Employee
        async function handleCreateEmployee(e) {
            e.preventDefault();
            const payload = {
                emp_code: document.getElementById('new-emp-code').value.trim(),
                name: document.getElementById('new-emp-name').value.trim(),
                email: document.getElementById('new-emp-email').value.trim(),
                department: document.getElementById('new-emp-dept').value.trim(),
                shift_start: document.getElementById('new-emp-start').value.trim(),
                shift_end: document.getElementById('new-emp-end').value.trim(),
                joined_on: document.getElementById('new-emp-joined').value.trim(),
            };

            try {
                const res = await fetch('/employees', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (res.status === 201) {
                    showAlert(`Employee ${payload.emp_code} created successfully!`);
                    document.getElementById('new-emp-modal').classList.add('hidden');
                    loadEmployees();
                } else {
                    showAlert(data.detail || 'Employee creation failed', false);
                }
            } catch (err) {
                showAlert('Error creating employee', false);
            }
        }

        // Load Analytics
        async function loadAnalytics() {
            const month = document.getElementById('analytics-month').value;
            if (!month) return;

            // 1. Department Summary
            try {
                const res = await fetch(`/analytics/departments/summary?month=${month}`);
                const data = await res.json();
                const container = document.getElementById('dept-summary-cards');
                container.innerHTML = '';
                
                if (data.items.length === 0) {
                    container.innerHTML = '<div class="col-span-full p-6 text-center text-slate-400 bg-white rounded-xl border border-slate-200">No department data for this month.</div>';
                }

                data.items.forEach(d => {
                    container.innerHTML += `
                        <div class="bg-white p-5 rounded-xl border border-slate-200 shadow-sm space-y-3">
                            <div class="flex items-center justify-between border-b pb-2">
                                <h5 class="font-bold text-slate-900">${d.department}</h5>
                                <span class="px-2 py-0.5 rounded bg-blue-50 text-blue-700 text-xs font-semibold">Headcount: ${d.headcount}</span>
                            </div>
                            <div class="grid grid-cols-2 gap-2 text-xs">
                                <div><span class="text-slate-500">Present Days:</span> <strong class="text-slate-800">${d.present_days}</strong></div>
                                <div><span class="text-slate-500">Avg Hours:</span> <strong class="text-slate-800">${d.avg_work_hours !== null ? d.avg_work_hours : 'N/A'}</strong></div>
                                <div><span class="text-slate-500">Late Count:</span> <strong class="text-rose-600">${d.late_count}</strong></div>
                                <div><span class="text-slate-500">Total Late:</span> <strong class="text-slate-800">${d.total_late_minutes}m</strong></div>
                            </div>
                        </div>
                    `;
                });
            } catch (err) {
                console.error(err);
            }

            // 2. Leaderboard
            try {
                const res = await fetch(`/analytics/leaderboard/late?month=${month}&limit=10`);
                const data = await res.json();
                const tbody = document.getElementById('leaderboard-tbody');
                tbody.innerHTML = '';

                if (data.items.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" class="p-6 text-center text-slate-400">No late records in this month! 🎉</td></tr>';
                    return;
                }

                data.items.forEach(l => {
                    let medal = l.rank === 1 ? '🥇' : l.rank === 2 ? '🥈' : l.rank === 3 ? '🥉' : '';
                    tbody.innerHTML += `
                        <tr class="hover:bg-slate-50/80 transition">
                            <td class="py-3 px-4 font-extrabold text-blue-600">${medal} #${l.rank}</td>
                            <td class="py-3 px-4 font-semibold text-slate-900">${l.name} <span class="text-xs font-mono text-slate-400">(${l.emp_code})</span></td>
                            <td class="py-3 px-4 text-slate-600 text-xs">${l.department}</td>
                            <td class="py-3 px-4 font-bold text-rose-600">${l.total_late_minutes} min</td>
                            <td class="py-3 px-4 text-slate-500">${l.late_count}</td>
                        </tr>
                    `;
                });
            } catch (err) {
                console.error(err);
            }
        }

        // Init
        loadEmployees();
        loadAttendance();
        loadAnalytics();
    </script>
</body>
</html>"""
