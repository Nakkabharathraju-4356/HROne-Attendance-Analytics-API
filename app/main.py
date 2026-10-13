"""HROne Employee Attendance & Analytics API, contract v2.0.

Run from the repository root:
    uvicorn app.main:app --port 8000

Configuration is read from MONGO_URI and MONGO_DB. Real environment variables take
precedence over values in a local .env file.
"""
from __future__ import annotations
import calendar
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Existing process environment always wins over .env values.
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMP_RE = re.compile(r"^EMP\d{4,6}$")
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def ensure_indexes() -> None:
    """Idempotent indexes; unique indexes enforce correctness under concurrency."""
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)],
                           name="ix_employee_department_joined")
    employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)],
                           name="ix_employee_joined_department")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                            unique=True, name="uq_attendance_employee_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)],
                            name="ix_attendance_date_employee")
    attendance.create_index([("date", ASCENDING), ("status", ASCENDING)],
                            name="ix_attendance_date_status")
    attendance.create_index([("emp_code", ASCENDING), ("date", DESCENDING)],
                            name="ix_attendance_employee_date_desc")
    attendance.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)],
                            name="ix_attendance_date_late")


# ---- Time, validation, and serialization helpers ---------------------------------

def parse_date(value: str, field: str = "date") -> date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field} must be a valid calendar date")


def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not MONTH_RE.fullmatch(value):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def truncate_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    if type(value) is not int or value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be an integer epoch-milliseconds value in range")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "timestamp is out of range")


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def ist_datetime(day: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    d = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        d -= timedelta(days=1)
    return d


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str,
                         record_date: str) -> int:
    day = parse_date(record_date)
    start = ist_datetime(day, shift_start).astimezone(UTC)
    # Late is defined relative to the shift start, with a strict 10-minute grace.
    elapsed = (punch_in - start).total_seconds()
    if elapsed <= 600:
        return 0
    return max(0, int(elapsed // 60))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str,
                     record_date: str) -> int:
    day = parse_date(record_date)
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    end = ist_datetime(end_day, shift_end).astimezone(UTC)
    elapsed = int((punch_out - end).total_seconds() // 60)
    return elapsed if elapsed >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def half_day_for(work_hours: Optional[float]) -> bool:
    return work_hours is not None and Decimal(str(work_hours)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    ) < Decimal("4.50")


def round_half_up(value: Optional[float], places: int = 2) -> Optional[float]:
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def working_days(start: date, end: date) -> int:
    """Count weekdays inclusively without loading or iterating attendance records."""
    if end < start:
        return 0
    total = (end - start).days + 1
    weeks, remainder = divmod(total, 7)
    result = weeks * 5
    for i in range(remainder):
        if (start.weekday() + i) % 7 < 5:
            result += 1
    return result


def public_employee(doc: dict[str, Any]) -> dict[str, Any]:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["created_at"] = to_epoch_ms(result.get("created_at"))
    return result


def public_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    result = {k: v for k, v in doc.items() if k != "_id"}
    for field in ("punch_in", "punch_out"):
        result[field] = to_epoch_ms(result.get(field))
    result["late_minutes"] = int(result.get("late_minutes") or 0)
    result["overtime_minutes"] = int(result.get("overtime_minutes") or 0)
    result["half_day"] = bool(result.get("half_day", False))
    result["history"] = result.get("history") or []
    for entry in result["history"]:
        entry["at"] = to_epoch_ms(entry.get("at"))
        changes = entry.get("changes") or {}
        for change in changes.values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = to_epoch_ms(change[side])
        entry["changes"] = changes
    return result


def presence_value_expr() -> dict[str, Any]:
    return {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                      {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0.0]}


def month_bounds_strings(month: str) -> tuple[str, str]:
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat()


def require_employee(emp_code: str) -> dict[str, Any]:
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


# ---- Request models ----------------------------------------------------------------

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeCreate(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value: str) -> str:
        if not DATE_RE.fullmatch(value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class EpochBody(StrictModel):
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not (MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
            raise ValueError("timestamp must be epoch milliseconds in range")
        return value


class PunchInRequest(EpochBody):
    emp_code: str
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(EpochBody):
    emp_code: str


class RegularizeRequest(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_punch_timestamp(cls, value):
        if value is not None and not (MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
            raise ValueError("timestamp must be epoch milliseconds in range")
        return value


# ---- Shared Mongo aggregation builders ---------------------------------------------

def monthly_employee_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start, end = month_bounds_strings(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$addFields": {"_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
                presence_value_expr(), 0.0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]



def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict[str, Any]]:
    start, end = month_bounds_strings(month)

    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department

    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code", "joined": "$joined_on"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lt": ["$date", end]},
                                    {"$gte": ["$date", "$$joined"]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "overtime_minutes": 1,
                            "half_day": 1,
                        }
                    },
                ],
                "as": "_logs",
            }
        },
        {
            "$addFields": {
                
"_present_logs": {
    "$filter": {
        "input": "$_logs",
        "as": "log",
        "cond": {
            "$and": [
                {
                    "$in": [
                        "$$log.status",
                        ["PRESENT", "WFH", "ON_DUTY"],
                    ]
                },
                {
                    "$gte": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$$log.date"
                                }
                            }
                        },
                        2,
                    ]
                },
                {
                    "$lte": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$$log.date"
                                }
                            }
                        },
                        6,
                    ]
                },
            ]
        },
    }
},

                "_worked_logs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {
                                    "$in": [
                                        "$$log.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {"$ne": ["$$log.work_hours", None]},
                            ]
                        },
                    }
                },
                "_late_logs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {
                            "$gt": [
                                {"$ifNull": ["$$log.late_minutes", 0]},
                                0,
                            ]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": {"$literal": 1},
                "debug_log_count": {"$size": "$_logs"},
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$_present_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$ifNull": ["$$log.half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$_worked_logs",
                            "as": "log",
                            "in": "$$log.work_hours",
                        }
                    }
                },
                "work_hours_count": {"$size": "$_worked_logs"},
                "late_count": {"$size": "$_late_logs"},
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$_late_logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
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
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]




def leaderboard_pipeline(
    month: str,
    limit: int,
    department: Optional[str]
) -> list[dict[str, Any]]:
    start, end = month_bounds_strings(month)

    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start, "$lte": end},
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
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
    ]

    if department is not None:
        pipeline.append(
            {"$match": {"_employee.department": department}}
        )

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$_employee.name",
                "department": "$_employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
                "rank": 1,
            }
        },
    ])

    return pipeline



def trend_pipeline(department: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    start = datetime.combine(parse_date(start_date), time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(parse_date(end_date) + timedelta(days=1), time.min, tzinfo=UTC)
    # One anchor employee makes $densify able to emit days even when there are no logs.
    return [
        {"$match": {"department": department}},
        {"$sort": {"emp_code": 1}},
        {"$limit": 1},
        {"$set": {"trend_date": {"$literal": start}}},
        {"$densify": {"field": "trend_date", "range": {
            "step": 1, "unit": "day", "bounds": [start, end_exclusive]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$trend_date"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]},
                {"$lte": ["$joined_on", {"$dateToString": {
                    "format": "%Y-%m-%d", "date": "$$day", "timezone": "UTC"}}]},
            ]}}},
            {"$count": "n"}], "as": "_headcount"}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": {"$dateToString": {
            "format": "%Y-%m-%d", "date": "$trend_date", "timezone": "UTC"}}},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code",
                    "foreignField": "emp_code", "as": "_emp"}},
                {"$unwind": "$_emp"},
                {"$match": {"_emp.department": department}},
            ], "as": "_day_logs"}},
        {"$set": {
            "date_string": {"$dateToString": {"format": "%Y-%m-%d", "date": "$trend_date", "timezone": "UTC"}},
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.n", 0]}, 0]},
            "present_count": {"$sum": {"$map": {"input": {"$filter": {"input": "$_day_logs", "as": "l",
                "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]}}},
                "as": "l", "in": {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]}}}},
            "late_count": {"$size": {"$filter": {"input": "$_day_logs", "as": "l",
                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$trend_date"}, 2]},
                                          {"$lte": [{"$dayOfWeek": "$trend_date"}, 6]}]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"trend_date": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"trend_date": 1}},
        {"$project": {"_id": 0, "date": "$date_string", "is_working_day": 1,
            "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


def explain_aggregate(collection, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    # Raw server explain, JSON-safe Extended JSON conversion at the API boundary.
    raw = db.command({"explain": {"aggregate": collection.name, "pipeline": pipeline,
                                  "cursor": {}}, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw))


# ---- System and employee endpoints -------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = employees.count_documents(query)
    docs = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(
        (page - 1) * page_size).limit(page_size)
    return {"items": [public_employee(d) for d in docs], "total": total,
            "page": page, "page_size": page_size}


# ---- Attendance endpoints ------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an integer epoch-milliseconds value")
    emp = require_employee(body.emp_code)
    punched_at = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(punched_at, emp["shift_start"], emp["shift_end"]).isoformat()
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": punched_at, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], emp["shift_end"], day),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an integer epoch-milliseconds value")
    emp = require_employee(body.emp_code)
    punched_at = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent open record with punch-in no later than this punch-out; works across midnight.
    record = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
                                 sort=[("punch_in", DESCENDING)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    if punched_at <= record["punch_in"] or punched_at > record["punch_in"] + timedelta(hours=24):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    work_hours = compute_work_hours(record["punch_in"], punched_at)
    update = {
        "punch_out": punched_at, "work_hours": work_hours,
        "overtime_minutes": compute_overtime(punched_at, emp["shift_start"], emp["shift_end"], record["date"]),
        "half_day": half_day_for(work_hours),
    }
    # Conditional update is the concurrency guard: only one simultaneous request wins.
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return public_attendance(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if date_from or date_to:
        query["date"] = {}
        if date_from: query["date"]["$gte"] = date_from
        if date_to: query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    total = attendance.count_documents(query)
    docs = attendance.find(query, {"_id": 0}).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_attendance(d) for d in docs], "total": total,
            "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    if "punch_in" in body.model_fields_set and body.punch_in is None:
        raise HTTPException(422, "punch_in must be an integer epoch-milliseconds value")
    if "punch_out" in body.model_fields_set and body.punch_out is None:
        raise HTTPException(422, "punch_out must be an integer epoch-milliseconds value")
    target_day = parse_date(date).isoformat()
    require_employee(emp_code)
    current = attendance.find_one({"emp_code": emp_code, "date": target_day})
    if not current:
        raise HTTPException(404, "attendance record not found")
    old = {k: current.get(k) for k in (
        "status", "punch_in", "punch_out", "work_hours", "late_minutes",
        "overtime_minutes", "half_day")}
    old["late_minutes"] = int(old["late_minutes"] or 0)
    old["overtime_minutes"] = int(old["overtime_minutes"] or 0)
    old["half_day"] = bool(old["half_day"] or False)
    old["history"] = current.get("history") or []
    status = body.status if body.status is not None else current["status"]
    pin = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else current.get("punch_in")
    pout = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else current.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot be combined with punch times")
        pin = None
        pout = None
    else:
        if pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        emp = require_employee(emp_code)
        if attendance_date(pin, emp["shift_start"], emp["shift_end"]).isoformat() != target_day:
            raise HTTPException(422, "punch_in must remain on the record's attendance date")
        if pout is not None and (pout <= pin or pout > pin + timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    emp = require_employee(emp_code)
    late = compute_late_minutes(pin, emp["shift_start"], emp["shift_end"], target_day) if pin else 0
    hours = compute_work_hours(pin, pout) if pin and pout else None
    overtime = compute_overtime(pout, emp["shift_start"], emp["shift_end"], target_day) if pin and pout else 0
    half = half_day_for(hours)
    new = {"status": status, "punch_in": pin, "punch_out": pout, "work_hours": hours,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    changes: dict[str, Any] = {}
    for key, new_value in new.items():
        old_value = old.get(key)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "regularization does not change the record")
    at = now_utc()
    history_entry = {"at": at, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap on the current history and mutable fields prevents silent lost updates.
    selector = {"_id": current["_id"]}
    if "history" in current:
        selector["history"] = current["history"]
    else:
        selector["history"] = {"$exists": False}
    for key in ("status", "punch_in", "punch_out"):
        selector[key] = current.get(key)
    updated = attendance.find_one_and_update(selector,
        {"$set": new, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry regularization")
    return public_attendance(updated)


# ---- Analytics endpoints: all record calculations are MongoDB aggregations ---------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = require_employee(emp_code)
    start, end = parse_month(month)
    first = max(start, parse_date(emp["joined_on"], "joined_on"))
    wd = working_days(first, end)
    rows = list(attendance.aggregate(monthly_employee_pipeline(emp_code, month)))
    row = rows[0] if rows else {}
    present = round_half_up(row.get("present_days", 0), 2) or 0.0
    pct = round_half_up((present / wd * 100), 2) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd,
        "present_days": present, "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                       department: Optional[str] = None):
    parse_month(month)
    items = list(employees.aggregate(department_summary_pipeline(month, department), allowDiskUse=True))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2) or 0.0
        item["avg_work_hours"] = round_half_up(item.get("avg_work_hours"), 2)
        for key in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            item[key] = int(item.get(key, 0))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    parse_month(month)
    items = list(attendance.aggregate(leaderboard_pipeline(month, limit, department), allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"),
                     to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must be >= from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    items = list(employees.aggregate(trend_pipeline(department, start.isoformat(), end.isoformat()),
                                     allowDiskUse=True))
    # MongoDB $densify includes the start but treats upper bound as exclusive.
    for item in items:
        item["attendance_rate"] = round_half_up(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = round_half_up(item.get("moving_avg_7d"), 4)
        item["present_count"] = round_half_up(item.get("present_count", 0), 2) or 0.0
        item["headcount"] = int(item.get("headcount", 0))
        item["late_count"] = int(item.get("late_count", 0))
    return {"department": department, "items": items}


# ---- Explain plans ------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code is not None: query["emp_code"] = emp_code
        if date_from:
            parse_date(date_from, "date_from")
        if date_to:
            parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        if date_from or date_to:
            query["date"] = {}
            if date_from: query["date"]["$gte"] = date_from
            if date_to: query["date"]["$lte"] = date_to
        if status is not None: query["status"] = status
        # Explain the same find/sort/skip/limit shape as the endpoint.
        cmd = {"explain": {"find": "attendance_logs", "filter": query,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size,
            "limit": page_size}, "verbosity": "executionStats"}
        raw = db.command(cmd)
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        parse_month(month)
        pipeline = monthly_employee_pipeline(emp_code, month)
        raw = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline,
                                      "cursor": {}}, "verbosity": "executionStats"})
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        parse_month(month)
        pipeline = department_summary_pipeline(month, department)
        raw = db.command({"explain": {"aggregate": "employees", "pipeline": pipeline,
                                      "cursor": {}}, "verbosity": "executionStats"})
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        parse_month(month)
        pipeline = leaderboard_pipeline(month, limit, department)
        raw = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline,
                                      "cursor": {}}, "verbosity": "executionStats"})
        collection = "attendance_logs"
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        pipeline = trend_pipeline(department, start.isoformat(), end.isoformat())
        raw = db.command({"explain": {"aggregate": "employees", "pipeline": pipeline,
                                      "cursor": {}}, "verbosity": "executionStats"})
        collection = "employees"
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(raw))}
