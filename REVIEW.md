# REVIEW.md

| # | Where | What is wrong | How to notice it | Fix in this implementation |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses naive local datetimes and ignores the record's attendance date and overnight shifts. | Punch in at 00:30 for a 22:00–06:00 shift. | Normalize instants to UTC, derive the IST attendance date, and compare against the shift start on that date. |
| 2 | `compute_late_minutes` | `int(total_seconds()/60)` can truncate negative values toward zero and the original grace logic does not explicitly encode the strict boundary. | Test exactly 09:40:00 and 09:40:01 for a 09:30 shift. | Apply a strict `elapsed > 600` check, then floor whole elapsed minutes. |
| 3 | `compute_work_hours` | Python `round` uses ties-to-even and binary floating-point can produce the wrong half-cent rounding. | Test durations whose hour value is exactly halfway between hundredths. | Use `Decimal` and `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Always uses the attendance date for shift end, so overnight shifts compare against the wrong day; it also omits the 30-minute threshold. | A 22:00–06:00 shift punching out at 06:29 and 06:30. | Move overnight shift end to the following day and return overtime only at 30 whole minutes or more. |
| 5 | Mongo client setup | Default PyMongo datetimes are naive and the starter mixes them with local system time. | Run on a machine in a non-IST timezone and inspect stored instants. | Use UTC-aware instants consistently and truncate to whole seconds. |
| 6 | `/health` | Always returns success without pinging MongoDB. | Stop MongoDB and request `/health`. | Ping MongoDB and return 503 on database failure. |
| 7 | `create_employee` | Check-then-insert is race-prone; duplicate employee codes can be inserted concurrently without a unique index. | Send parallel POST requests for one `emp_code`. | Create a unique index and map `DuplicateKeyError` to 409. |
| 8 | `list_employees` | `skip` is `page * page_size`, skipping the first page; `total` ignores the department filter. | Request page 1 and compare `total` with a department-filtered count. | Use `(page - 1) * page_size` and count the filtered query. |
| 9 | `list_employees` | Does not validate page bounds or cap page size. | Try page 0 or page size 1000. | Apply FastAPI query constraints. |
| 10 | `punch_in` | Does not return 404 for an unknown employee before accessing shift data. | Punch in with an unknown employee code. | Resolve the employee explicitly and return 404. |
| 11 | `punch_in` | Converts milliseconds using machine-local time, does not reject seconds/floats/out-of-range values, and assigns dates incorrectly for overnight shifts. | Send a seconds-looking timestamp or punch in shortly after midnight for an overnight shift. | Validate strict integer epoch milliseconds, normalize to UTC, and calculate the IST attendance date. |
| 12 | `punch_in` | Check-then-insert allows duplicate attendance records under concurrent requests, and it leaks MongoDB `_id` as `id`. | Send simultaneous punches for the same employee/date. | Enforce a unique compound index on `(emp_code, date)` and return no internal ID. |
| 13 | `list_attendance` | Loads and sorts the entire collection in Python, which is too slow for 100,000 records; pagination cannot use indexes. | Seed a large collection and measure memory/time. | Filter, sort, count, skip and limit in MongoDB using indexes. |
| 14 | `list_attendance` | Sorts only by date and returns `_id` as `id`, contrary to the contract. | Insert multiple employees on the same date and inspect output fields. | Sort by date descending then employee code ascending; omit `_id`. |
| 15 | Analytics endpoints | The starter has no implementations for punch-out, regularization, monthly summary, department summary, leaderboard, trend, or explain. | Call each path in the contract. | Implement all contract paths and use MongoDB aggregation pipelines for analytics. |
| 16 | All responses | Datetimes would be returned as JSON date strings rather than epoch milliseconds; legacy missing fields are not normalized. | Inspect output for timestamps and seeded legacy records. | Serialize instants to epoch milliseconds and supply the contract defaults for legacy fields. |

## Reviewed and retained

- The two-collection model (`employees` and `attendance_logs`) and natural attendance key `(emp_code, date)` are retained because the contract specifies them.
- Presence statuses remain `PRESENT`, `WFH`, and `ON_DUTY`; `ABSENT` and `LEAVE` do not count as presence.
- Derived attendance values are computed when a record is written and read as stored values by analytics, as required.
