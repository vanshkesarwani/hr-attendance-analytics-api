# DECISIONS.md

1. **Indexes:** On `employees`: `{emp_code: 1}` (unique) enforces identity; `{department: 1, emp_code: 1}` and `{joined_on: 1, department: 1}` optimize listings and headcount. On `attendance_logs`: `{emp_code: 1, date: 1}` (unique) prevents duplicate daily logs and backs monthly queries; `{date: -1, emp_code: 1}` serves cursor pagination; `{emp_code: 1, punch_in: -1}` backs punch-out; `{date: 1, late_minutes: 1, emp_code: 1}` indexes the late leaderboard. Rejected: indexing history array elements since audit logs are never queried directly.

2. **Punch-in race:** Both concurrent requests resolve the IST date and attempt `insert_one`. The unique index on `(emp_code, date)` ensures MongoDB commits only one. The winner returns `201 Created`; the second triggers a `DuplicateKeyError` (E11000), returning `409 Conflict`.

3. **Ties:** Handled inside MongoDB using `$setWindowFields` with standard competition `$rank` (1, 2, 2, 4). Filtering `rank <= limit` occurs after ranking, so all tied employees at the cutoff threshold are returned.

4. **Headcount:** The aggregation starts from `employees` matching `joined_on <= month_end`, calculating headcount before performing a `$lookup` on `attendance_logs`. Employees without any attendance logs still contribute to the headcount.

5. **100x scale change:** Maintain pre-computed daily department rollups updated via MongoDB Change Streams instead of calculating cross-collection joins and 92-day rolling averages dynamically per request.
