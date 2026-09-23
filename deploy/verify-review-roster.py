import json
import sqlite3
import sys
from pathlib import Path


db_path = Path(sys.argv[1]).resolve()
roster_path = Path(sys.argv[2]).resolve()
roster = json.loads(roster_path.read_text(encoding="utf-8"))

expected = {"member": {}, "team_lead": {}, "supervisor": {}}
for center_item in roster["centers"]:
    center = center_item["center"]
    expected["supervisor"][center_item["supervisor"]] = (center, "")
    for group in center_item["groups"]:
        group_name = group["group_name"]
        if group.get("team_lead"):
            expected["team_lead"][group["team_lead"]] = (center, group_name)
        for member in group.get("members", []):
            expected["member"][member] = (center, group_name)

with sqlite3.connect(db_path) as connection:
    rows = connection.execute(
        """
        SELECT role_code, user_name, center, group_name
        FROM review_role_assignments
        WHERE active = 1 AND source = 'organization_sync'
        """
    ).fetchall()
    actual = {"member": {}, "team_lead": {}, "supervisor": {}}
    for role_code, user_name, center, group_name in rows:
        if role_code in actual:
            actual[role_code][user_name] = (center or "", group_name or "")
    flags = connection.execute(
        "SELECT naming_enabled, ai_redline_enabled, enabled FROM review_workflow_config WHERE id = 1"
    ).fetchone()

assert actual == expected, {
    role: {
        "missing_or_wrong": sorted(set(expected[role].items()) - set(actual[role].items())),
        "unexpected_or_wrong": sorted(set(actual[role].items()) - set(expected[role].items())),
    }
    for role in expected
    if actual[role] != expected[role]
}
assert tuple(flags or ()) == (0, 0, 0), flags
assert not {"舒豪", "赵佳乐"} & set(actual["supervisor"])

print(
    json.dumps(
        {
            "counts": {role: len(people) for role, people in actual.items()},
            "supervisors": actual["supervisor"],
            "flags": list(flags),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
)
