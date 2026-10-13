# DECISIONS.md

1. **Indexes.** I use a unique `employees.emp_code` index for employee identity, and a unique `(emp_code, date)` attendance index to prevent duplicate daily records even when requests race. Department plus join date supports headcount lookups. Attendance indexes cover date-sorted listing, employee/month lookups, date/status filtering, and monthly late totals. I avoided indexing every field: extra indexes increase write cost and storage without helping the contract's query patterns.

2. **Punch-in race.** Both requests may reach the insert at once, but MongoDB's unique compound index is the authority. One insert succeeds with 201. The competing insert raises `DuplicateKeyError`, which the API converts to 409. A pre-check alone would not be safe.

3. **Ties.** MongoDB `$rank` ranks only by total late minutes, so tied totals receive the same rank. I filter on `rank <= limit` after ranking, then sort by late minutes descending and employee code ascending. This can return more than `limit` rows.

4. **Headcount.** Department summary starts from employees who joined on or before the month's last day, then looks up that employee's logs. Employees with no logs still produce a row and count toward headcount; employees who join later are excluded.

5. **100x data.** I would measure real explain plans and query latency first, then consider pre-aggregated daily summaries and background refreshes for expensive analytics. I would keep raw attendance as the audit source of truth and validate summary consistency.
