"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from typing_extensions import Annotated

import json
from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Setup TLS certificate bundle if certifi is available (required on macOS Python)
try:
    import certifi
    ca_file = certifi.where()
except ImportError:
    ca_file = None

mongo_kwargs: Dict[str, Any] = {"tz_aware": True}
if ca_file:
    mongo_kwargs["tlsCAFile"] = ca_file

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), **mongo_kwargs)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Helpers for Instants, Date, and Calculations (R1 - R8)
# --------------------------------------------------------------------------- #
def truncate_ms(ms: int) -> int:
    """Truncate epoch milliseconds to whole seconds (R1)."""
    return (ms // 1000) * 1000


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC timezone-aware datetime, truncated to whole seconds."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert timezone-aware or naive UTC datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def current_epoch_ms() -> int:
    """Current server time in epoch milliseconds truncated to whole seconds."""
    return (int(datetime.now(timezone.utc).timestamp())) * 1000


def parse_time_str(t_str: str) -> Tuple[int, int]:
    h, m = map(int, t_str.split(":"))
    return h, m


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start indicates an overnight shift."""
    sh, sm = parse_time_str(shift_start)
    eh, em = parse_time_str(shift_end)
    return (eh, em) <= (sh, sm)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in.
    Overnight shifts: a punch-in whose IST time is earlier than shift_end belongs
    to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        eh, em = parse_time_str(shift_end)
        end_time = time(eh, em)
        if punch_in_ist.time() < end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 (> 600s)
    after shift_start; then late_minutes = whole minutes (floor) since shift_start.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    att_date = date.fromisoformat(attendance_date)
    sh, sm = parse_time_str(shift_start)
    shift_start_dt = datetime.combine(att_date, time(sh, sm), tzinfo=IST)
    diff_sec = int((punch_in_ist - shift_start_dt).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    hours = Decimal(diff_sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if at least 30; otherwise 0.
    Overnight shifts end on attendance_date + 1 day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    att_date = date.fromisoformat(attendance_date)
    eh, em = parse_time_str(shift_end)
    if is_overnight_shift(shift_start, shift_end):
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date
    shift_end_dt = datetime.combine(shift_end_date, time(eh, em), tzinfo=IST)
    diff_sec = int((punch_out_ist - shift_end_dt).total_seconds())
    if diff_sec >= 1800:
        return diff_sec // 60
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """R7: Mon-Fri working days between start_date and end_date inclusive."""
    start = date.fromisoformat(start_date_str)
    end = date.fromisoformat(end_date_str)
    if start > end:
        return 0
    cur = start
    count = 0
    one_day = timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:  # Monday=0, Friday=4
            count += 1
        cur += one_day
    return count


# --------------------------------------------------------------------------- #
# Document Serializers
# --------------------------------------------------------------------------- #
def serialize_history(history_list: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    res = []
    for h in (history_list or []):
        entry = {
            "at": dt_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for k, v in h.get("changes", {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = dt_to_epoch_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = dt_to_epoch_ms(to_val)
            entry["changes"][k] = {"from": from_val, "to": to_val}
        res.append(entry)
    return res


def serialize_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)
    d["punch_in"] = dt_to_epoch_ms(d.get("punch_in"))
    d["punch_out"] = dt_to_epoch_ms(d.get("punch_out"))
    d["work_hours"] = d.get("work_hours")
    d["late_minutes"] = d.get("late_minutes", 0)
    d["overtime_minutes"] = d.get("overtime_minutes", 0)
    d["half_day"] = d.get("half_day", False)
    d["history"] = serialize_history(d.get("history", []))
    return d


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)
    d["created_at"] = dt_to_epoch_ms(d.get("created_at"))
    return d


# --------------------------------------------------------------------------- #
# Pydantic Schemas matching openapi.yaml
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


EmployeeIn = EmployeeCreate  # Backward compatibility alias


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


PunchInIn = PunchInRequest  # Backward compatibility alias


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


class HistoryChange(BaseModel):
    from_: Optional[Any] = Field(None, alias="from")
    to: Optional[Any] = None

    class Config:
        populate_by_name = True


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Any]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


# --------------------------------------------------------------------------- #
# MongoDB Indexes and Lifespan Startup
# --------------------------------------------------------------------------- #
def create_indexes() -> None:
    """Create all required indexes idempotently at startup."""
    # employees indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)])

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create all indexes idempotently
    create_indexes()
    yield


# --------------------------------------------------------------------------- #
# FastAPI App Initialization
# --------------------------------------------------------------------------- #
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Starter Endpoints (Fixed)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, response_model=Employee)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee with code '{body.emp_code}' already exists")

    return serialize_employee(doc)


@app.get("/employees", response_model=EmployeePage)
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{body.emp_code}' not found")

    # R1: Instants truncated to whole seconds
    if body.punched_at is not None:
        punched_ms = truncate_ms(body.punched_at)
        punch_in_dt = epoch_ms_to_dt(punched_ms)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # R1: Attendance date in IST, accounting for overnight shifts
    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])

    # R2: Compute late minutes
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], emp["shift_end"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee '{body.emp_code}' is already punched in for date '{att_date}'")

    return serialize_attendance_record(doc)


@app.get("/attendance", response_model=AttendancePage)
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Status] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
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
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Punch-Out & Regularization Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out", response_model=AttendanceRecord)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{body.emp_code}' not found")

    if body.punched_at is not None:
        punched_ms = truncate_ms(body.punched_at)
        punch_out_dt = epoch_ms_to_dt(punched_ms)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No matching punch-in found for employee")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # 422 if punched_at <= punch_in or more than 24 hours after it
    if punch_out_dt <= punch_in_dt or (punch_out_dt - punch_in_dt) > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in and within 24 hours")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_doc:
        raise HTTPException(status_code=409, detail="Record was already punched out concurrently")

    return serialize_attendance_record(updated_doc)


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord)
def regularize_attendance(
    emp_code: str = Path(..., description="Employee business code"),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="The record's attendance date, YYYY-MM-DD."),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{emp_code}' not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail=f"Attendance record not found for '{emp_code}' on '{date}'")

    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)
    old_history = record.get("history") or []

    # Target new status
    new_status = body.status if body.status is not None else old_status

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
    elif new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(truncate_ms(body.punch_in))
        else:
            new_punch_in = old_punch_in

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(truncate_ms(body.punch_out))
        else:
            new_punch_out = old_punch_out

        # Rule: a presence status requires a punch_in
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # Rule: punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail=f"punch_in falls on attendance date {calc_date}, which does not match record date {date}")

        # Rule: punch_out must be after punch_in and within 24 hours
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in or (new_punch_out - new_punch_in) > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    # Recompute derived fields
    if new_status in ("PRESENT", "WFH", "ON_DUTY"):
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], emp["shift_end"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False
    else:
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False

    # Track changes
    changes: Dict[str, Any] = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    # Rule: a request that changes nothing is 422
    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrency safe: match history size to prevent lost updates
    update_filter = {
        "_id": record["_id"],
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(old_history)]},
    }
    updated_doc = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent regularization detected; please retry")

    return serialize_attendance_record(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Aggregation Pipelines)
# --------------------------------------------------------------------------- #
import calendar


def get_month_date_range(month_str: str) -> Tuple[str, str]:
    year, month = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    return f"{month_str}-01", f"{month_str}-{last_day:02d}"


def round_half_up(val: Optional[Union[float, Decimal]], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly)
def employee_monthly(
    emp_code: str = Path(..., description="Employee business code"),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM."),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{emp_code}' not found")

    month_start, month_end = get_month_date_range(month)

    # R7: Working days calculated from joined_on for mid-month joiners
    effective_start = max(month_start, emp["joined_on"])
    working_days = count_working_days(effective_start, month_end) if emp["joined_on"] <= month_end else 0

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    row = res[0] if res else {}

    present_days = float(row.get("present_days", 0.0))
    leave_days = int(row.get("leave_days", 0))
    late_count = int(row.get("late_count", 0))
    total_late_minutes = int(row.get("total_late_minutes", 0))
    total_overtime_minutes = int(row.get("total_overtime_minutes", 0))

    if working_days > 0:
        pct_val = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal(100)
        attendance_pct = float(pct_val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM."),
    department: Optional[str] = Query(None, description="Restrict to one department (exact match)."),
):
    month_start, month_end = get_month_date_range(month)

    match_employees: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_employees["department"] = department

    pipeline = [
        {"$match": match_employees},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$l.work_hours", None]},
                                            ]
                                        },
                                        "$$l.work_hours",
                                        None,
                                    ]
                                },
                            }
                        },
                        "as": "wh",
                        "cond": {"$ne": ["$$wh", None]},
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "all_work_hours": {"$push": "$work_hours_list"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "flattened": {
                                "$reduce": {
                                    "input": "$all_work_hours",
                                    "initialValue": [],
                                    "in": {"$concatArrays": ["$$value", "$$this"]},
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$flattened"}, 0]},
                                {"$avg": "$$flattened"},
                                None,
                            ]
                        },
                    }
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for item in raw_items:
        item["present_days"] = float(item["present_days"])
        item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
        items.append(item)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM."),
    limit: int = Query(10, ge=1, le=50, description="Rank cutoff"),
    department: Optional[str] = Query(None, description="Restrict to one department"),
):
    month_start, month_end = get_month_date_range(month)

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
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
                "as": "emp_doc",
            }
        },
        {"$unwind": "$emp_doc"},
    ]

    if department:
        pipeline.append({"$match": {"emp_doc.department": department}})

    # Standard competition ranking: ties share a rank, next rank is skipped (1, 2, 2, 4)
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp_doc.name",
                "department": "$emp_doc.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", response_model=Trend)
def department_trend(
    department: str = Path(..., description="Department name"),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$", description="Start date, YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$", description="End date, YYYY-MM-DD"),
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail=f"Department '{department}' not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    dt_from = datetime.combine(d_from, time(0, 0), tzinfo=timezone.utc)
    # Upper bound for densify is exclusive, so add 1 day to d_to
    dt_to_bound = datetime.combine(d_to + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date_dt": {"$literal": dt_from}}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from, dt_to_bound],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"emp_code": 1, "joined_on": 1}},
                ],
                "as": "dept_employees",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date", "dept_emps": "$dept_employees.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$cur_date"]},
                                    {"$in": ["$emp_code", "$$dept_emps"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_dt"},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "raw_moving_avg": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": "$raw_moving_avg",
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for item in raw_items:
        item["present_count"] = float(item["present_count"])
        item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
        items.append(item)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
ExplainEndpointName = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


@app.get("/admin/explain/{endpoint}", response_model=ExplainResponse)
def explain_endpoint(
    endpoint: ExplainEndpointName = Path(..., description="Endpoint to explain"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Status] = Query(None),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

        q: Dict[str, Any] = {}
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
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        raw_exp = cursor.explain()
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")

        month_start, month_end = get_month_date_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$in": [
                                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ]
        raw_exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        month_start, month_end = get_month_date_range(month)
        match_employees: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department:
            match_employees["department"] = department

        pipeline = [
            {"$match": match_employees},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "department": 1,
                    "emp_code": 1,
                    "present_days": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {
                                                    "$in": [
                                                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                            }
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$ifNull": ["$$l.late_minutes", 0]},
                            }
                        }
                    },
                    "leave_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                            }
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                            }
                        }
                    },
                    "work_hours_list": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": "$logs",
                                    "as": "l",
                                    "in": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$$l.work_hours", None]},
                                                ]
                                            },
                                            "$$l.work_hours",
                                            None,
                                        ]
                                    },
                                }
                            },
                            "as": "wh",
                            "cond": {"$ne": ["$$wh", None]},
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$present_days"},
                    "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {"$sum": "$total_late_minutes"},
                    "leave_count": {"$sum": "$leave_count"},
                    "on_duty_count": {"$sum": "$on_duty_count"},
                    "all_work_hours": {"$push": "$work_hours_list"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                    "avg_work_hours": {
                        "$let": {
                            "vars": {
                                "flattened": {
                                    "$reduce": {
                                        "input": "$all_work_hours",
                                        "initialValue": [],
                                        "in": {"$concatArrays": ["$$value", "$$this"]},
                                    }
                                }
                            },
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$size": "$$flattened"}, 0]},
                                    {"$avg": "$$flattened"},
                                    None,
                                ]
                            },
                        }
                    },
                }
            },
            {"$sort": {"department": ASCENDING}},
        ]
        raw_exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        month_start, month_end = get_month_date_range(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
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
                    "as": "emp_doc",
                }
            },
            {"$unwind": "$emp_doc"},
        ]
        if department:
            pipeline.append({"$match": {"emp_doc.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp_doc.name",
                    "department": "$emp_doc.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ])
        raw_exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required for department_trend",
            )

        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        dt_from = datetime.combine(d_from, time(0, 0), tzinfo=timezone.utc)
        dt_to_bound = datetime.combine(d_to + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)

        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"date_dt": {"$literal": dt_from}}},
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [dt_from, dt_to_bound],
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                    "date_dt": 1,
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "pipeline": [
                        {"$match": {"department": department}},
                        {"$project": {"emp_code": 1, "joined_on": 1}},
                    ],
                    "as": "dept_employees",
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"cur_date": "$date", "dept_emps": "$dept_employees.emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$date", "$$cur_date"]},
                                        {"$in": ["$emp_code", "$$dept_emps"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "day_logs",
                }
            },
            {
                "$project": {
                    "date": 1,
                    "is_working_day": {
                        "$in": [
                            {"$dayOfWeek": "$date_dt"},
                            [2, 3, 4, 5, 6],
                        ]
                    },
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": "$dept_employees",
                                "as": "e",
                                "cond": {"$lte": ["$$e.joined_on", "$date"]},
                            }
                        }
                    },
                    "present_count": {
                        "$sum": {
                            "$map": {
                                "input": "$day_logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$map": {
                                "input": "$day_logs",
                                "as": "l",
                                "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                            }
                        }
                    },
                }
            },
            {
                "$project": {
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_working_day", True]},
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    },
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "raw_moving_avg": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, "current"]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": "$raw_moving_avg",
                }
            },
            {"$sort": {"date": ASCENDING}},
        ]
        raw_exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "employees"

    else:
        raise HTTPException(status_code=422, detail=f"Unsupported endpoint: {endpoint}")

    clean_explain = json.loads(json_util.dumps(raw_exp))
    return JSONResponse(
        status_code=200,
        content={
            "endpoint": endpoint,
            "collection": collection,
            "explain": clean_explain,
        },
    )
