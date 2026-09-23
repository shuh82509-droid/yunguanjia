import unittest
from types import SimpleNamespace
from app.platform_audit import material_audit_receipt, retain_material_audit

class PlatformAuditTests(unittest.TestCase):
    def test_only_exact_native_rejection_is_rejected(self):
        for raw, expected in [('REJECT', 'REJECT'), ('PASS', 'PASS'), ('IN_PROGRESS', 'IN_PROGRESS'), ('timeout', 'UNKNOWN'), ('', 'UNKNOWN')]:
            receipt = material_audit_receipt([{'video_id': 'v', 'audit_status': raw, 'delivery_not_reason': ['原因']}], 'v', 'req')
            self.assertEqual(receipt['status'], expected)
            self.assertEqual(receipt['segments'], [])
            self.assertEqual(receipt['localization'], 'not_provided')
        self.assertEqual(material_audit_receipt([{'video_id': 'other', 'audit_status': 'REJECT'}], 'v'), {})

    def test_missing_readback_preserves_previous_receipt_and_binding(self):
        task = SimpleNamespace(platform_asset_id='v', binding_evidence={'video_id': 'v', 'platform_audit': {'status': 'REJECT'}})
        retain_material_audit(task, {})
        self.assertEqual(task.binding_evidence['platform_audit']['status'], 'REJECT')
        retain_material_audit(task, {'platform_audit': {'status': 'PASS', 'video_id': 'other'}})
        self.assertEqual(task.binding_evidence['platform_audit']['status'], 'REJECT')
        retain_material_audit(task, {'platform_audit': {'status': 'PASS', 'video_id': 'v'}})
        self.assertEqual(task.binding_evidence['video_id'], 'v')
        self.assertEqual(task.binding_evidence['platform_audit']['status'], 'PASS')
