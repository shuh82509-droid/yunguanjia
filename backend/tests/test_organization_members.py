import unittest
from unittest.mock import MagicMock, patch

from app.main import _canonical_organization_center, _organization_dashboard_scope, _scoped_organization_members


class OrganizationMemberScopeTests(unittest.TestCase):
    def test_full_oa_path_is_normalized_to_center(self):
        self.assertEqual(_canonical_organization_center("品牌营销部-AI营销中心"), "AI营销中心")
        self.assertEqual(_canonical_organization_center("品牌营销部-营销中心J"), "营销中心J")

    @patch("app.main.is_permission_manager", return_value=False)
    def test_director_receives_department_scope(self, _permission_manager):
        access = _organization_dashboard_scope({"number": "FD-D", "realName": "赵佳乐", "parentDept": "品牌营销部"})
        self.assertEqual(access["scope"], "department")

    @patch("app.main.is_permission_manager", return_value=False)
    def test_latest_leader_overrides_receive_center_scope(self, _permission_manager):
        ai_access = _organization_dashboard_scope({"number": "FD-AI", "realName": "吴为", "parentDept": "品牌营销部", "deptName": "营销中心J"})
        j_access = _organization_dashboard_scope({"number": "FD-J", "realName": "张鑫露", "parentDept": "品牌营销部"})
        self.assertEqual(ai_access["centers"], ["AI营销中心"])
        self.assertEqual(j_access["centers"], ["营销中心J"])

    @patch("app.main.is_permission_manager", return_value=True)
    def test_leader_scope_is_not_widened_by_permission_admin_role(self, _permission_manager):
        access = _organization_dashboard_scope({"number": "FD-AI", "realName": "吴为", "parentDept": "品牌营销部"})
        self.assertEqual(access["scope"], "center")
        self.assertEqual(access["centers"], ["AI营销中心"])

    @patch("app.main.is_permission_manager", return_value=False)
    def test_ordinary_member_with_oa_center_remains_personal(self, _permission_manager):
        access = _organization_dashboard_scope(
            {"number": "FD-MEMBER", "realName": "普通同事", "parentDept": "品牌营销部", "deptName": "AI营销中心"}
        )
        self.assertEqual(access["scope"], "personal")
        self.assertEqual(access["centers"], [])

    @patch("app.main._organization_members")
    @patch("app.main._organization_dashboard_scope")
    def test_center_scope_filters_members_on_server(self, scope, members):
        scope.return_value = {"scope": "center", "centers": ["营销中心J"], "personName": "张鑫露"}
        members.return_value = [
            {"personName": "张鑫露", "center": "营销中心J", "leaderCenters": ["营销中心J"], "isLeader": True},
            {"personName": "同事甲", "center": "营销中心J", "leaderCenters": [], "isLeader": False},
            {"personName": "同事乙", "center": "营销中心B", "leaderCenters": [], "isLeader": False},
        ]

        access, visible = _scoped_organization_members(MagicMock(), {"number": "FD-J", "realName": "张鑫露"})

        self.assertEqual(access["scope"], "center")
        self.assertEqual([item["personName"] for item in visible], ["张鑫露", "同事甲"])

    def test_center_scope_includes_governed_roster_without_enriched_access_rows(self):
        db = MagicMock()
        empty_result = MagicMock()
        empty_result.all.return_value = []
        db.scalars.return_value = empty_result

        access, visible = _scoped_organization_members(db, {"number": "FD-J-LEAD", "realName": "张鑫露"})

        self.assertEqual(access["centers"], ["营销中心J"])
        self.assertTrue(all(item["center"] == "营销中心J" for item in visible))
        self.assertIn("陈凯涛", [item["personName"] for item in visible])
        self.assertIn("唐茹", [item["personName"] for item in visible])


if __name__ == "__main__":
    unittest.main()
