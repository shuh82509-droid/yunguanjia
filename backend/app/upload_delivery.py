"""Upload registration proofs; no user-supplied owner or content SHA is trusted."""
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from .models import Asset, UploadSession, WorkstationReturn


# The public same-origin relay has a 40 MiB request limit. Existing sessions
# retain their part_size and receipts; only newly created sessions use this size.
UPLOAD_PART_BYTES = 32 * 1024 * 1024


def remember_direct_ticket(db, ticket, *, filename, size, content_type,
                           owner_number, owner_name, asset_scope, category):
    """Persist ownership before returning a signed URL to an existing client."""
    now = datetime.utcnow()
    session = UploadSession(
        id=str(uuid4()), owner_number=owner_number, owner_name=owner_name,
        sha256="", file_size=size, filename=filename, content_type=content_type,
        asset_scope=asset_scope, category=category, object_key=ticket["object_key"],
        multipart_upload_id="", part_size=UPLOAD_PART_BYTES,
        status="direct_pending", created_at=now, updated_at=now,
        expires_at=now + timedelta(seconds=int(ticket["expires_in"])),
    )
    db.add(session)
    db.commit()
    return {**ticket, "session_id": session.id}


def registration_proof(db, payload, owner_number, remote, canonical_category,
                       *, workstation_record=None):
    """Return (asset, session, replay), refusing any unsupported ownership claim.

    A legacy registered asset can be read back by its recorded owner. An old
    unregistered direct upload without a ticket record cannot prove ownership
    from a display-name directory (different OA accounts may share a name).
    Such a file is preserved for reconciliation, never silently taken over.
    """
    key = payload.object_key
    asset = db.scalar(select(Asset).where(Asset.object_key == key))
    session = db.scalar(select(UploadSession).where(UploadSession.object_key == key))
    if asset and (asset.deleted_at is not None or asset.purged_at is not None):
        raise HTTPException(409, "该文件在回收站或已删除，不能重复登记；请先核对原素材")
    if session and session.owner_number != owner_number:
        raise HTTPException(403, "只能登记本人上传会话对应的文件")
    if asset and asset.uploaded_by_number and asset.uploaded_by_number != owner_number:
        raise HTTPException(403, "该文件已属于其他上传人，不能重复登记或改写归属")
    if getattr(payload, "session_id", None) and (
        not session or payload.session_id != session.id
    ):
        raise HTTPException(409, "上传会话与文件不一致，请恢复原上传记录")
    if not remote or remote.get("object_key") != key or int(remote.get("size") or 0) <= 0:
        raise HTTPException(409, "原文件尚未完整上传，请继续原上传任务")

    category = canonical_category(payload.category)
    if session:
        direct = not session.multipart_upload_id
        allowed_states = {"completed", "direct_pending"} if direct else {"completed"}
        if session.status not in allowed_states:
            raise HTTPException(409, "上传会话尚未完成，请先完成原文件上传")
        if int(remote["size"]) != session.file_size or payload.asset_scope != session.asset_scope:
            raise HTTPException(409, "原文件大小或素材库与上传会话不一致")
        # Older desktop presign requests contain no category. Only those
        # server-created tickets have an empty category; bind it at completion.
        if session.category and canonical_category(session.category) != category:
            raise HTTPException(409, "产品与上传会话不一致，请核对原上传产品")
    elif workstation_record is not None:
        record = db.get(WorkstationReturn, workstation_record.idempotency_key)
        provenance = dict(record.provenance or {}) if record else {}
        maker = str(provenance.get("maker_id") or "SERVICE-WIS-REMIX")[:80]
        if (not record or record.object_key != key or record.file_size != int(remote["size"])
                or maker != owner_number or payload.asset_scope != "marketing_video"
                or canonical_category(provenance.get("category")) != category):
            raise HTTPException(409, "工作台回传记录与文件、上传人或产品不一致")
    elif not (asset and asset.uploaded_by_number == owner_number):
        raise HTTPException(409, "缺少原上传归属记录，文件已保留；请核对原上传回执后恢复，勿重复上传")

    replay = bool(asset and asset.uploaded_by_number == owner_number)
    if replay:
        # A completion retry is not a metadata editor and cannot replace bytes.
        if (asset.size != int(remote["size"]) or asset.modified_at != remote.get("modified_at")
                or (asset.etag and asset.etag != str(remote.get("etag") or ""))):
            raise HTTPException(409, "已登记原文件发生变化，请核对原版本；不会覆盖已有交付")
        if not session and workstation_record is None and (
            asset.asset_scope != payload.asset_scope
            or canonical_category(asset.category) != category
        ):
            raise HTTPException(409, "重复登记与原素材库或产品不一致，请直接打开原素材")
    return asset, session, replay


def finish_registration(session, category):
    if session is not None:
        now = datetime.utcnow()
        session.status = "completed"
        session.completed_at = session.completed_at or now
        session.updated_at = now
        session.category = session.category or category
