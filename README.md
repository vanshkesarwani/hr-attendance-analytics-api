# Employee Attendance & Analytics API (FastAPI + MongoDB)

A high-performance attendance tracking and analytics API built with FastAPI and MongoDB (6.0+).

## Tech Stack
* **Language & Framework**: Python 3.11+, FastAPI, Uvicorn
* **Database**: MongoDB 6.0+ (PyMongo)
* **Validation**: Pydantic v2

## Setup & Running Locally

1. **Create virtual environment & install dependencies**:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment variables**:
   Set `MONGO_URI` and `MONGO_DB` in your environment or via a local `.env`:
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Seed sample data (optional)**:
   ```bash
   python sample_seed.py
   ```

4. **Start the API server**:
   ```bash
   uvicorn app.main:app --port 8000
   ```
   The service will be ready at `http://localhost:8000`. Readiness check is available at `GET /health`.

## Key Features & Guarantees
* **Idempotent Index Creation**: All unique and compound indexes are created automatically during the application lifespan startup.
* **Race-Safe Concurrency**: Punch-in and employee creation rely on unique indexes to prevent duplicate key race conditions, returning `409 Conflict`. Punch-out and regularization use atomic operations with optimistic concurrency control.
* **Pure Aggregation Analytics**: Employee monthly summary, department summaries, late-comers leaderboard (with standard competition ranking ties), and 7-day department trends with gap-filling (`$densify`) are calculated entirely inside MongoDB aggregation pipelines without in-memory Python loops.
* **Query Performance**: All query plans are verified via `GET /admin/explain/{endpoint}`, using index scans (`IXSCAN`) with zero collection scans (`COLLSCAN`).

## Documentation Files
* `REVIEW.md`: Comprehensive defect review of the starter code with concrete failing examples and fixes.
* `DECISIONS.md`: Design rationale covering indexes, race conditions, tie handling, headcount accounting, and scale strategies.
