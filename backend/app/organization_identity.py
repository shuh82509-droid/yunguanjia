"""Conservative staff identity joins for organization and timesheet records."""
import re
from collections import defaultdict

def employee_key(value) -> str:
    value = str(value or "").strip().upper()
    match = re.fullmatch(r"([A-Z]{2})-?(\d{3,8})", value)
    return f"{match[1]}-{match[2]}" if match else value

def person_key(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().casefold()

def personal_identity_match(item: dict, number: str, name: str) -> bool:
    """An authenticated stable number never falls back to somebody's name."""
    key = employee_key(number)
    if key:
        return employee_key(item.get("employeeId")) == key
    return not employee_key(item.get("employeeId")) and person_key(item.get("personName")) == person_key(name)

def canonical_members(members: list[dict]) -> list[dict]:
    seen, result = set(), []
    for item in members:
        number, name = employee_key(item.get("employeeId")), person_key(item.get("personName"))
        key = ("number", number) if number else ("name", name)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

def match_timesheets(members: list[dict], sources: list[dict]) -> list[dict | None]:
    """At most one source row per member and one member per source row.

    Number matches take precedence. A name fallback is allowed only when the
    name identifies exactly one row on both sides and neither known number
    conflicts. Duplicate source numbers/names remain unassigned, not guessed.
    """
    member_numbers, source_numbers, member_names, source_names = (defaultdict(list) for _ in range(4))
    for index, item in enumerate(members):
        number, name = employee_key(item.get("employeeId")), person_key(item.get("personName"))
        if number: member_numbers[number].append(index)
        if name: member_names[name].append(index)
    for index, item in enumerate(sources):
        number, name = employee_key(item.get("employeeId")), person_key(item.get("personName"))
        if number: source_numbers[number].append(index)
        if name: source_names[name].append(index)
    result, used = [None] * len(members), set()
    for number, indices in member_numbers.items():
        matches = source_numbers.get(number, [])
        if len(indices) == len(matches) == 1:
            index, source = indices[0], matches[0]
            result[index] = sources[source]
            used.add(source)
    for name, indices in member_names.items():
        matches = source_names.get(name, [])
        if len(indices) != 1 or len(matches) != 1:
            continue
        index, source = indices[0], matches[0]
        if result[index] is not None or source in used:
            continue
        left, right = employee_key(members[index].get("employeeId")), employee_key(sources[source].get("employeeId"))
        if left and right:
            continue
        # Do not assign a duplicate-number source through a name loophole.
        if right and len(source_numbers[right]) != 1:
            continue
        result[index] = sources[source]
        used.add(source)
    return result
