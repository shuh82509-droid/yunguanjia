"""Owner-confirmed departures override stale OA/department/permission records.

Preserve historical grants; identify only by a stable staff number, never a name.
These three departures were explicitly confirmed in this task on 2026-09-08.
"""
import re

CONFIRMED_DEPARTURES = {
    "OD-005620": "吴梓逸",
    "FD-028611": "刘炜琳",
    "OD-006169": "胡小甜",
}

def staff_number(user: dict) -> str:
    for field in ("number", "user_number", "userNumber", "employeeId", "userId", "id", "username", "identifier"):
        value = user.get(field)
        if not isinstance(value, str):
            continue
        cleaned = value.strip().upper()
        if cleaned.startswith("NUMBER:"):
            cleaned = cleaned[7:]
        match = re.fullmatch(r"([A-Z]{2})-?(\d{3,8})", cleaned)
        if match:
            return f"{match[1]}-{match[2]}"
    return ""

def confirmed_departure(user: dict) -> str | None:
    return CONFIRMED_DEPARTURES.get(staff_number(user))
