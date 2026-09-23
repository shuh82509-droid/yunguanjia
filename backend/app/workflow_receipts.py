"""Read-only receipt projection. No uploads, retries, platform calls or database writes."""


def _timestamp(value):
    return value.isoformat() + "Z" if value is not None else None


def _platform_audit(evidence):
    audit = evidence.get("platform_audit")
    if not isinstance(audit, dict):
        return {}
    # Only saved platform audit facts are projected; never copy unknown fields,
    # perform a platform call, or reinterpret upload/association success as PASS.
    value = {key: audit[key][:500] for key in ("status", "video_id", "source", "request_id", "read_at") if isinstance(audit.get(key), str)}
    for key, limit in (("raw_statuses", 20), ("reasons", 20)):
        items = audit.get(key)
        value[key] = [item[:500] for item in items[:limit] if isinstance(item, str)] if isinstance(items, list) else []
    return value


def receipt_payload(row, platform):
    common = {
        "id": row.id,
        "asset_id": row.asset_id,
        "status": row.status,
        "created_by_number": row.created_by_number,
        "updated_at": _timestamp(row.updated_at),
        "source": "cloud_manager_saved_platform_readback",
    }
    if platform == "qianchuan":
        evidence = row.binding_evidence if isinstance(row.binding_evidence, dict) else {}
        return {
            **common,
            "advertiser_id": row.advertiser_id,
            "plan_id": row.plan_id,
            "platform_asset_id": row.platform_asset_id,
            "binding_verified_at": _timestamp(row.binding_verified_at),
            "binding_evidence": {**{key: evidence.get(key) for key in ("matched_count", "video_id", "advertiser_id", "plan_id")}, "platform_audit": _platform_audit(evidence)},
        }
    if platform == "wechat_channels":
        return {
            **common,
            "account_id": row.account_id,
            "platform_export_id": row.platform_export_id,
            "platform_export_verified_at": _timestamp(row.platform_export_verified_at),
            "platform_content_url": row.platform_content_url,
        }
    raise ValueError("unsupported platform")
