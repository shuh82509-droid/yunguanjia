import unittest

from app.timesheet_service import TimesheetService


class TimesheetServiceTests(unittest.TestCase):
    def test_parse_records_uses_governed_effective_hour_definition(self):
        records = [
            {
                "fields": {
                    "姓名": "同事甲",
                    "工号-新": "FD-001",
                    "当月工时归属部门": ["品牌营销部"],
                    "当月工时归属中心": ["营销中心J"],
                    "当月岗位二级分类": ["岛民"],
                    "当月所属部门分类": "营销",
                    "平均工作时长-新": 9.25,
                    "总工作时长-新": 175.75,
                    "排班天数": 19,
                    "打卡天数": 18,
                    "平均出勤时长": 11.7,
                    "工时统计（排除异常工时）": "统计",
                    "公司工时统计": "统计",
                    "部门工时统计": "统计",
                }
            },
            {
                "fields": {
                    "姓名": "异常记录",
                    "当月工时归属部门": ["品牌营销部"],
                    "当月工时归属中心": ["营销中心J"],
                    "当月岗位二级分类": ["岛民"],
                    "当月所属部门分类": "营销",
                    "平均工作时长-新": 3.0,
                    "工时统计（排除异常工时）": "不统计",
                    "公司工时统计": "统计",
                    "部门工时统计": "统计",
                }
            },
            {
                "fields": {
                    "姓名": "其他部门",
                    "当月工时归属部门": ["产品供应部"],
                    "当月岗位二级分类": ["岛民"],
                    "当月所属部门分类": "供应链",
                    "平均工作时长-新": 9.0,
                    "工时统计（排除异常工时）": "统计",
                    "公司工时统计": "统计",
                    "部门工时统计": "统计",
                }
            },
        ]

        members = TimesheetService._parse_records(records, "2026年8月")

        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["personName"], "同事甲")
        self.assertEqual(members[0]["center"], "营销中心J")
        self.assertEqual(members[0]["averageEffectiveHours"], 9.25)

    def test_summary_keeps_missing_values_out_of_denominator(self):
        summary = TimesheetService._summary([
            {"averageEffectiveHours": 9.0, "averageAttendanceHours": 11.0, "scheduledDays": 19, "punchDays": 18, "totalEffectiveHours": 171},
            {"averageEffectiveHours": 10.0, "averageAttendanceHours": None, "scheduledDays": 19, "punchDays": 19, "totalEffectiveHours": 190},
        ])

        self.assertEqual(summary["averageEffectiveHours"], 9.5)
        self.assertEqual(summary["averageAttendanceHours"], 11.0)
        self.assertEqual(summary["totalEffectiveHours"], 361.0)

    def test_member_index_prefers_employee_id_and_also_supports_name(self):
        by_employee, by_name = TimesheetService.index_members({
            "members": [{"personName": "舒 豪", "employeeId": "fd-026222"}]
        })

        self.assertIn("FD-026222", by_employee)
        self.assertIn("舒豪", by_name)


if __name__ == "__main__":
    unittest.main()
