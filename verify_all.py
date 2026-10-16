import urllib.request
import json
import sys

endpoints = [
    '/health',
    '/employees',
    '/attendance',
    '/analytics/employees/EMP0001/monthly?month=2026-07',
    '/analytics/departments/summary?month=2026-07',
    '/analytics/leaderboard/late?month=2026-07',
    '/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12',
    '/admin/explain/attendance_list',
    '/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07',
    '/admin/explain/department_summary?month=2026-07',
    '/admin/explain/late_leaderboard?month=2026-07',
    '/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-12'
]

for ep in endpoints:
    try:
        req = urllib.request.Request(f'http://localhost:8000{ep}')
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            print(f'[OK] {resp.status} : {ep}', flush=True)
    except Exception as e:
        print(f'[ERROR] : {ep} -> {e}', flush=True)
