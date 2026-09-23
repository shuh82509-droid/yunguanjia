import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.personal_sales_service import PEOPLE_PATH, PersonalSalesService, SourcePendingError, build_snapshot


class PersonalSalesSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.people = [
            {"name": "向可可", "employeeId": "FD-1", "department": "营销中心J", "keywords": ["向可可", "kk"]},
            {"name": "陈凯涛", "employeeId": "FD-2", "department": "营销中心J", "keywords": ["陈凯涛", "kt"]},
        ]
        self.freshness = {
            "QIANCHUAN_STANDARD": "2026-08-10",
            "QIANCHUAN_CHENGFANG": "2026-08-10",
            "HAITUN": "2026-08-10",
            "ADQ": "2026-08-10",
        }

    @staticmethod
    def row(platform, material_id, material_name, gmv, channel="视频号", internal_author=""):
        return {
            "report_date": "2026-08-10",
            "channel": channel,
            "source_platform": platform,
            "advertiser_id": "account-1",
            "material_id": material_id,
            "material_name": material_name,
            "internal_author": internal_author,
            "gmv_yuan": gmv,
            "source_cutoff_at": "2026-08-10 23:59:59",
            "source_updated_at": "2026-08-11 01:00:00",
        }

    def test_snapshot_excludes_zero_and_conflicts_without_forcing_attribution(self):
        rows = [
            self.row("HAITUN", "1", "护肤好物#kk", 100),
            self.row("HAITUN", "2", "护肤好物#kk", 50),
            self.row("CHENGFANG", "3", "kt (1).mp4", 200, channel="千川"),
            self.row("HAITUN", "4", "合作素材 kk kt", 40),
            self.row("STANDARD", "5", "没有署名.mp4", 60, channel="千川"),
            self.row("ADQ", "6", "零成交-kk", 0),
        ]

        snapshot = build_snapshot(rows, self.people, date(2026, 8, 10), self.freshness)

        self.assertEqual(snapshot["quality"]["totalSourceGmvYuan"], 450)
        self.assertEqual(snapshot["quality"]["mappedGmvYuan"], 350)
        self.assertEqual(snapshot["quality"]["conflictGmvYuan"], 40)
        self.assertEqual(snapshot["quality"]["unmappedGmvYuan"], 60)
        self.assertEqual([item["personName"] for item in snapshot["rankings"]], ["陈凯涛", "向可可"])
        self.assertEqual(snapshot["rankings"][1]["materialCount"], 1)

    def test_exact_internal_author_has_priority_over_title_keywords(self):
        rows = [
            self.row(
                "STANDARD",
                "7",
                "标题里出现kt.mp4",
                88,
                channel="千川",
                internal_author="向可可",
            )
        ]

        snapshot = build_snapshot(rows, self.people, date(2026, 8, 10), self.freshness)

        self.assertEqual(snapshot["rankings"][0]["personName"], "向可可")
        self.assertEqual(snapshot["rankings"][0]["totalGmvYuan"], 88)
        self.assertEqual(snapshot["quality"]["conflictMaterialCount"], 0)

    def test_latest_creator_roster_is_complete_and_unique(self):
        people = json.loads(PEOPLE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(len(people), 75)
        self.assertEqual(len({item["name"] for item in people}), len(people))
        self.assertEqual(len({item["employeeId"] for item in people}), len(people))
        self.assertTrue(
            {
                "曾颖",
                "何金芝",
                "陈俞婷",
                "张华杰",
                "赵一卓",
                "林晓丽",
            }.issubset({item["name"] for item in people})
        )
        for person in people:
            self.assertTrue(person["name"].strip())
            self.assertTrue(person["employeeId"].strip())
            self.assertTrue(person["department"].strip())
            self.assertEqual(person["keywords"][0], person["name"])
            normalized = [keyword.strip().lower() for keyword in person["keywords"]]
            self.assertEqual(len(normalized), len(set(normalized)))

    def test_shared_short_code_is_reported_as_conflict(self):
        people = json.loads(PEOPLE_PATH.read_text(encoding="utf-8"))
        snapshot = build_snapshot(
            [self.row("STANDARD", "shared-lxl", "campaign-lxl.mp4", 99, channel="千川")],
            people,
            date(2026, 8, 10),
            self.freshness,
        )

        self.assertEqual(snapshot["quality"]["conflictMaterialCount"], 1)
        self.assertEqual(snapshot["quality"]["mappedMaterialCount"], 0)
        self.assertEqual(
            set(snapshot["conflictMaterials"][0]["candidateNames"]),
            {"林星龙", "林晓丽"},
        )

    def test_single_letter_alias_is_not_force_attributed(self):
        people = json.loads(PEOPLE_PATH.read_text(encoding="utf-8"))
        snapshot = build_snapshot(
            [self.row("HAITUN", "single-letter", "campaign-M.mp4", 66)],
            people,
            date(2026, 8, 10),
            self.freshness,
        )

        self.assertEqual(snapshot["quality"]["unmappedMaterialCount"], 1)
        self.assertEqual(snapshot["quality"]["mappedMaterialCount"], 0)

    def test_daily_range_is_recalculated_from_preserved_rows_and_pending_is_not_zero(self):
        first = self.row("STANDARD", "daily-1", "向可可-日报1.mp4", 100, channel="千川")
        second = self.row("STANDARD", "daily-2", "向可可-日报2.mp4", 250, channel="千川")
        first["report_date"] = "2026-08-09"
        second["report_date"] = "2026-08-10"
        snapshot = build_snapshot([first, second], self.people, date(2026, 8, 10), self.freshness)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "personal-sales.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            service = PersonalSalesService()
            service.snapshot_path = path

            selected = service.get_snapshot(date(2026, 8, 10), date(2026, 8, 10))
            self.assertEqual(selected["meta"]["periodStart"], "2026-08-10")
            self.assertEqual(selected["meta"]["periodEnd"], "2026-08-10")
            self.assertEqual(selected["rankings"][0]["totalGmvYuan"], 250)
            self.assertEqual(selected["rankings"][0]["materialCount"], 1)
            self.assertNotIn("periodRows", selected)
            self.assertNotIn("periodRows", service.get_snapshot())

            with self.assertRaises(SourcePendingError):
                service.get_snapshot(date(2026, 8, 11), date(2026, 8, 11))

    def test_snapshot_file_is_parsed_once_until_mtime_changes(self):
        snapshot = build_snapshot(
            [self.row("STANDARD", "cached-1", "向可可-缓存.mp4", 100, channel="千川")],
            self.people,
            date(2026, 8, 10),
            self.freshness,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "personal-sales.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            service = PersonalSalesService()
            service.snapshot_path = path

            first = service._current_snapshot()
            second = service._current_snapshot()

            self.assertIs(first, second)
            self.assertEqual(service.complete_through_date(), date(2026, 8, 10))


if __name__ == "__main__":
    unittest.main()
