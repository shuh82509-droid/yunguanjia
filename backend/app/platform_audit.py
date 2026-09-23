"""Exact platform audit receipts; never infer a rejection from delivery errors.

Enum/source: oceanengine/ad_open_sdk_go models/
model_qianchuan_uni_promotion_ad_material_get_v1_0_data_ad_material_infos_audit_status.go
The material endpoint has no cut-level time ranges. Do not invent them.
"""
from datetime import datetime, timezone


def material_audit_receipt(rows: list[dict], video_id: str, request_id: str = '') -> dict:
    matches = [row for row in rows if str(row.get('video_id') or '') == str(video_id)]
    if not matches:
        return {}
    states = sorted({str(row.get('audit_status') or '') for row in matches})
    status = 'REJECT' if 'REJECT' in states else 'IN_PROGRESS' if 'IN_PROGRESS' in states else 'PASS' if states == ['PASS'] else 'UNKNOWN'
    reasons = list(dict.fromkeys(str(reason)[:500] for row in matches for reason in (row.get('delivery_not_reason') or []) if isinstance(reason, str)))[:20]
    return {'status': status, 'raw_statuses': states, 'reasons': reasons, 'video_id': str(video_id),
            'source': 'qianchuan/uni_promotion/ad/material/get', 'request_id': request_id,
            'read_at': datetime.now(timezone.utc).isoformat(), 'localization': 'not_provided', 'segments': []}


def retain_material_audit(task, result: dict) -> None:
    receipt = result.get('platform_audit')
    if receipt and receipt.get('video_id') == str(task.platform_asset_id):
        task.binding_evidence = {**(task.binding_evidence or {}), 'platform_audit': receipt}
