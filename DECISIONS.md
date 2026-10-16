# DECISIONS.md

1. **Indexes.**
   I created four indexes at startup. First, a unique index on `employees.emp_code`—this is the primary identity key and enforces race-safe uniqueness for concurrent creates (catching `DuplicateKeyError` instead of check-then-insert). Second, a unique compound index on `attendance_logs(emp_code, date)`—the natural key of every record; it makes point-lookups instant and ensures at most one punch-in per employee per day without a separate pre-check. Third, a compound index on `attendance_logs(date DESC, emp_code ASC)` to serve the list endpoint's required sort order efficiently on 100k docs. Fourth, a compound index on `attendance_logs(emp_code, date, status)` to support analytics month-range queries. I considered a separate index on `late_minutes` for the leaderboard but rejected it: the pipeline already filters on `date` (covered by existing indexes) and `late_minutes > 0` is a secondary filter that benefits from the date index reducing the working set first.

2. **Punch-in race.**
   Both requests arrive at the same time, both pass employee lookup, both compute the attendance date, and both call `insert_one`. Because the unique compound index `(emp_code, date)` is enforced by MongoDB at the storage layer, exactly one insert succeeds; the other throws `DuplicateKeyError`. The winning request returns 201; the losing request catches the exception and returns 409. No application-level lock or pre-check is needed.

3. **Ties.**
   `$rank` (standard competition ranking) assigns the same rank to tied employees, then skips the next rank. After ranking, I filter `rank <= limit`. If two employees are tied exactly at rank `limit`, both are included even though the response has more than `limit` rows—this is the specified behaviour ("the response can therefore contain more than `limit` rows when employees tie at a rank <= `limit`").

4. **Headcount with zero logs.**
   The department summary pipeline starts from the `employees` collection (not `attendance_logs`), filters those with `joined_on <= last_day_of_month`, then uses `$lookup` with `$unwind` (`preserveNullAndEmptyArrays: true`) to join their logs. Employees who have no logs produce an unwound document with a null `logs` field, contributing 1 to the headcount and 0 to all other aggregated fields. They are never excluded.

5. **One change for 100× the data.**
   I would shard `attendance_logs` on `{emp_code: 1, date: 1}`, which spreads both the primary unique-key writes and the most common range queries evenly across shards. The analytics pipelines already push as much work as possible into early `$match` stages (which hit the shard key), so they would naturally parallelize across shards without requiring cross-shard scatter-gather for per-employee or per-month queries.
