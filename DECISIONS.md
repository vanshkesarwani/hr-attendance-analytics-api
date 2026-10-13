# DECISIONS.md

1. **Indexes.**
On `employees`: `{emp_code: 1}` (unique) enforces employee uniqueness; `{department: 1, emp_code: 1}` serves department listings; `{joined_on: 1, department: 1}` backs headcount scans.
On `attendance_logs`: `{emp_code: 1, date: 1}` (unique) guarantees one record per employee per day and backs employee monthly lookups; `{date: -1, emp_code: 1}` serves paginated attendance listings; `{emp_code: 1, punch_in: -1}` backs punch-out; `{date: 1, late_minutes: 1, emp_code: 1}` powers the late leaderboard.
*Rejected:* An index on `history.at` or individual history fields, as history is an append-only audit trail never queried as a search target.

2. **Punch-in race.**
Both requests resolve the IST attendance date and attempt `insert_one`. Under the unique index on `(emp_code, date)`, MongoDB commits exactly one insert. The winning request returns `201 Created`, while the concurrent request hits a `DuplicateKeyError` (E11000), which the code catches and returns as `409 Conflict`.

3. **Ties.**
Standard competition ranking (1, 2, 2, 4) is computed in the database using `$setWindowFields` with `$rank`. Filtering `rank <= limit` occurs *after* ranking, so tied employees at the cutoff are all included. The response may exceed `limit` rows, ordered by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
The pipeline starts from `employees` matching `joined_on <= month_end`, grouping by department and counting headcount via `$sum: 1` before performing a `$lookup` into `attendance_logs`. Employees with zero logs still contribute to headcount.

5. **One thing you would change for 100x data.**
At 100x scale (~10 million records), computing on-the-fly cross-collection summaries and 92-day moving averages per HTTP request would degrade throughput. I would maintain pre-aggregated daily department rollups updated via MongoDB Change Streams or a background job.
