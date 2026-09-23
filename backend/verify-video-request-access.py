"""Read-only production verification; never assign tasks or send notifications."""
import json
from sqlalchemy import select, text
from app.database import SessionLocal
from app.main import user_permissions, video_request_assignees, _organization_dashboard_scope
from app.models import OaAccessGrant

EXPECTED = {
    "FD-021068": "何雨庭", "FD-026565": "张锦玲", "FD-023018": "陈广聪",
    "OD-000256": "赖健诚", "FD-026487": "何金芝", "FD-027130": "马鸿涛",
    "FD-027853": "曾颖", "FD-020772": "陈俞婷", "FD-028363": "黄小华",
    "FD-025988": "赖鸣悦", "FD-021099": "古广妹",
}
assert _organization_dashboard_scope({"realName": "丁小恬", "department": "总经办", "center": "总助模块"})["scope"] == "department"
with SessionLocal() as db:
    db.execute(text("PRAGMA query_only=ON"))
    checked = {}
    for number in ["FD-026222", "FD-021068", "FD-117110"]:
        grant = db.scalars(select(OaAccessGrant).where(OaAccessGrant.user_number == number,
                                                     OaAccessGrant.active.is_(True))).first()
        assert grant, f"Active OA identity missing: {number}"
        user = {"number": number, "realName": grant.real_name, "groupName": grant.department}
        permissions = user_permissions(user)
        assert permissions["video_request_assigner"]
        assert permissions["video_request_supervisor_viewer"]
        result = video_request_assignees(q="", db=db, user=user)
        actual = {item["number"]: item["name"] for item in result["items"]}
        assert EXPECTED.items() <= actual.items(), actual
        assert "FD-117110" not in actual and "赵佳乐" not in actual.values()
        checked[grant.real_name] = {"can_assign": True, "assignee_count": result["total"]}
    print(json.dumps({"verified": checked, "assignees": actual, "read_only": True}, ensure_ascii=False))
