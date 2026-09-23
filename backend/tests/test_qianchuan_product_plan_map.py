import unittest

from app.qianchuan_product_plan_map import product_plan_map_snapshot


class QianchuanProductPlanMapTests(unittest.TestCase):
    def test_required_product_bundles_use_reviewed_account_ids(self):
        snapshot = product_plan_map_snapshot()
        bundles = {item["key"]: item for item in snapshot["items"]}

        self.assertEqual(
            {rule["advertiser_id"] for rule in bundles["deep_sea_ampoule"]["rules"]},
            {"1760222277505102", "1758788274050062", "1864699162217866"},
        )
        self.assertEqual(
            {rule["advertiser_id"] for rule in bundles["protein_spray"]["rules"]},
            {
                "1760222277505102",
                "1758788274050062",
                "1864699162217866",
                "1869672250595328",
            },
        )
        spray = bundles["protein_spray"]
        spray_fixed = [rule for rule in spray["rules"] if rule["match_mode"] == "exact_plan"]
        self.assertEqual(
            spray_fixed,
            [
                {
                    "advertiser_id": "1869672250595328",
                    "advertiser_name": "营销部WIS-厚拓（爱创）-2",
                    "scope": "multiplication",
                    "match_mode": "exact_plan",
                    "keyword": "",
                    "plan_id": "1873739958166842",
                }
            ],
        )
        self.assertEqual(
            [rule["keyword"] for rule in spray["rules"] if rule["match_mode"] == "keyword"],
            ["喷雾", "喷雾", "喷雾"],
        )
        black_crystal = bundles["black_crystal_mask"]
        fixed = [rule for rule in black_crystal["rules"] if rule["match_mode"] == "exact_plan"]
        self.assertEqual([rule["plan_id"] for rule in fixed], ["1870289794646204"])
        self.assertIn("黑晶", black_crystal["aliases"])

    def test_snapshot_is_not_mutable_across_requests(self):
        first = product_plan_map_snapshot()
        first["items"][0]["rules"].clear()
        second = product_plan_map_snapshot()
        self.assertTrue(second["items"][0]["rules"])

    def test_source_revision_matches_latest_reviewed_feishu_mapping(self):
        source = product_plan_map_snapshot()["source"]
        self.assertEqual(source["document_id"], "BX1nd6H4yo6hIqxUKvvcRKUMn8b")
        self.assertEqual(source["revision"], 536)


if __name__ == "__main__":
    unittest.main()
