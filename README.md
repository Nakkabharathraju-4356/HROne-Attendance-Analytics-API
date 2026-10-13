# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (MongoDB 7 recommended)
- Git

## Run locally (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

For MongoDB Atlas, set `MONGO_URI` to your Atlas connection string in the current PowerShell session. Replace the placeholder with your own connection string; do not commit or share it.

```powershell
$env:MONGO_URI = "YOUR_MONGODB_ATLAS_CONNECTION_STRING"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

The application supports a local `.env` file for development, but `.env` files and credentials must not be committed to Git.
## Important implementation notes
- All code is in `app/main.py`; indexes are created idempotently at application startup.
- Instants cross the API boundary as epoch milliseconds and are stored as BSON datetimes.
- Attendance analytics use MongoDB aggregation pipelines. `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day` are read from stored values in analytics.
- `REVIEW.md` describes defects found in the supplied starter. `DECISIONS.md` records the main design decisions.

## Before submitting
1. Start MongoDB and run the API locally.
2. Test normal and edge cases, including overnight shifts, weekends, no-log employees, ties in the leaderboard, corrections, and simultaneous requests.
3. Verify the explain plans against your own large test dataset.
4. Push only the required source and documentation to a public Git repository. Do not commit `.env`, credentials, virtual environments, sample problem statement, or private assignment materials.
5. Keep the repository public through 31 October 2026.
