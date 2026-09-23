"""Owner-only media vault, deliberately outside the public Asset/OSS catalog.

No private row or path is returned by the shared scanner, search, favorites,
reviews, analytics or publishing APIs. Media is streamed only after owner auth.
"""
import hashlib
import asyncio
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import BoundedSemaphore
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column
from starlette.concurrency import run_in_threadpool

from .config import settings
from .database import Base, get_db

CHUNK_BYTES = 8 * 1024 * 1024
MAX_BYTES = 2 * 1024 ** 3
FINALIZE_SLOTS = BoundedSemaphore(2)
MEDIA_TYPES = {'.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm',
               '.mkv': 'video/x-matroska', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.png': 'image/png', '.webp': 'image/webp'}


class PrivateAsset(Base):
    __tablename__ = 'private_assets'
    __table_args__ = (UniqueConstraint('owner_number', 'sha256', 'size', name='uq_private_owner_hash_size'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    owner_name: Mapped[str] = mapped_column(String(120), default='')
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(100), default='待分类')
    folder_name: Mapped[str] = mapped_column(String(160), default='')
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default='uploading', index=True)
    shared_asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PrivateUploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(gt=0, le=MAX_BYTES)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    category: str = Field(default='待分类', max_length=100)
    folder_name: str = Field(default='', max_length=160)


class PrivateMetadata(BaseModel):
    filename: str | None = Field(default=None, min_length=1, max_length=512)
    category: str | None = Field(default=None, max_length=100)
    folder_name: str | None = Field(default=None, max_length=160)


class PrivateShare(BaseModel):
    confirmed: bool = False


def vault_root() -> Path:
    configured = os.getenv('PRIVATE_ASSET_DATA_DIR', '').strip()
    database = make_url(settings.database_url).database or 'wis_video_center.db'
    root = Path(configured) if configured else Path(database).resolve().parent / 'private-assets'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root.resolve()


def media_path(asset_id: str) -> Path:
    try:
        canonical = str(UUID(asset_id))
    except ValueError as error:
        raise HTTPException(404, '私人素材不存在') from error
    return vault_root() / f'{canonical}.media'


@contextmanager
def asset_lock(asset_id: str):
    """OS lock protects app restarts/multiple processes without stale leases."""
    lock_path = media_path(asset_id).with_suffix('.lock')
    with open(lock_path, 'a+b') as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise HTTPException(409, '此文件正在处理，请稍后续传') from error
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def private_access_enabled(owner: str) -> bool:
    """Feature eligibility only; every route still requires authenticated ownership."""
    if os.getenv('PRIVATE_ASSETS_ENABLED', '').strip().lower() != 'true':
        return False
    if not owner or not owner.strip():
        return False
    if os.getenv('PRIVATE_ASSET_ACCESS_MODE', 'allowlist').strip().lower() == 'all_authorized':
        return True
    allowed = {value.strip() for value in os.getenv('PRIVATE_ASSET_ALLOWED_USERS', '').split(',') if value.strip()}
    return bool(owner) and owner in allowed


def owner_number(user: dict) -> str:
    owner = str(user.get('number') or user.get('userId') or user.get('id') or '').strip()
    if not owner or len(owner) > 80:
        raise HTTPException(403, '需要有效的 OA 工号才能使用私人素材')
    if not private_access_enabled(owner):
        raise HTTPException(403, '私人素材功能尚未对当前账号开放')
    return owner


def owned_asset(db: Session, asset_id: str, owner: str, *, allow_deleted=False) -> PrivateAsset:
    row = db.scalar(select(PrivateAsset).where(PrivateAsset.id == asset_id, PrivateAsset.owner_number == owner))
    if not row or (row.deleted_at is not None and not allow_deleted):
        raise HTTPException(404, '私人素材不存在')
    return row


def asset_out(row: PrivateAsset, *, offset=False) -> dict:
    result = {key: getattr(row, key) for key in ('id', 'filename', 'content_type', 'category', 'folder_name', 'size', 'status', 'shared_asset_id')}
    result.update(visibility='private', created_at=row.created_at.isoformat() + 'Z', updated_at=row.updated_at.isoformat() + 'Z')
    result['media_url'] = f'api/private-assets/{row.id}/media'
    if offset:
        file = media_path(row.id)
        result.update(offset=file.stat().st_size if file.exists() else 0, chunk_bytes=CHUNK_BYTES)
    return result


def safe_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or any(ord(c) < 32 or c in '/\\' for c in cleaned):
        raise HTTPException(400, '文件名无效')
    if Path(cleaned).suffix.lower() not in MEDIA_TYPES:
        raise HTTPException(400, '仅支持 MP4、MOV、WebM、MKV、JPG、PNG、WebP 素材，不支持工程文件')
    return cleaned


def create_upload(db: Session, user: dict, payload: PrivateUploadCreate) -> dict:
    owner = owner_number(user)
    filename = safe_name(payload.filename)
    row = db.scalar(select(PrivateAsset).where(PrivateAsset.owner_number == owner, PrivateAsset.sha256 == payload.sha256, PrivateAsset.size == payload.size))
    if row and row.deleted_at is not None:
        raise HTTPException(409, '同一私人文件已在回收区，请先恢复，避免重复录入')
    if row:
        if row.status == 'invalid_upload':
            with asset_lock(row.id):
                db.refresh(row)
                if row.status == 'invalid_upload' and not row.deleted_at:
                    row.status = 'uploading'
                    row.updated_at = datetime.utcnow()
                    db.commit()
        return {'asset': asset_out(row, offset=True), 'reused': row.status == 'ready'}
    if shutil.disk_usage(vault_root()).free < payload.size + 1024 ** 3:
        raise HTTPException(507, '私人存储可用空间不足，未改为公开上传')
    row = PrivateAsset(id=str(uuid4()), owner_number=owner, owner_name=str(user.get('realName') or user.get('name') or '')[:120],
                       filename=filename, content_type=MEDIA_TYPES[Path(filename).suffix.lower()], category=payload.category.strip() or '待分类',
                       folder_name=payload.folder_name.strip(), sha256=payload.sha256, size=payload.size)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return create_upload(db, user, payload)
    return {'asset': asset_out(row, offset=True), 'reused': False}


def append_chunk(db: Session, owner: str, asset_id: str, offset: int, content: bytes) -> dict:
    row = owned_asset(db, asset_id, owner)
    if row.status != 'uploading':
        raise HTTPException(409, '该素材已完成上传，禁止覆盖')
    if not content or len(content) > CHUNK_BYTES or offset < 0 or offset + len(content) > row.size:
        raise HTTPException(400, '上传分片范围无效')
    file = media_path(row.id)
    with asset_lock(row.id):
        db.refresh(row)
        if row.status != 'uploading' or row.deleted_at:
            raise HTTPException(409, '素材状态已改变')
        current = file.stat().st_size if file.exists() else 0
        if offset < current and offset + len(content) <= current:
            with open(file, 'rb') as stream:
                stream.seek(offset)
                if stream.read(len(content)) == content:
                    return asset_out(row, offset=True)
        if offset != current:
            raise HTTPException(409, '上传位置已变化，请读取进度后续传')
        with open(file, 'ab') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        row.updated_at = datetime.utcnow()
        db.commit()
    return asset_out(row, offset=True)


def validate_media(file: Path) -> None:
    try:
        result = subprocess.run([os.getenv('FFPROBE_PATH', 'ffprobe'), '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', str(file)],
                                capture_output=True, timeout=30, check=True, encoding='utf-8')
        streams = json.loads(result.stdout).get('streams', [])
        if not any(stream.get('codec_type') == 'video' for stream in streams):
            raise ValueError('missing video/image stream')
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise HTTPException(422, '文件校验未通过，未进入素材库；请检查视频或图片是否可正常打开') from error


def complete_upload(db: Session, owner: str, asset_id: str) -> dict:
    row = owned_asset(db, asset_id, owner)
    file = media_path(row.id)
    with finalize_slot(), asset_lock(row.id):
        db.refresh(row)
        if row.deleted_at:
            raise HTTPException(404, '私人素材不存在')
        if row.status == 'ready':
            return {'asset': asset_out(row), 'reused': True}
        if not file.exists() or file.stat().st_size != row.size:
            raise HTTPException(409, '文件尚未传完，请继续上传')
        with open(file, 'rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != row.sha256:
            # Preserve the invalid bytes for recovery; a retry reuses the same
            # private row but starts a clean upload, never overwrites ready media.
            file.rename(file.with_name(f'{row.id}.{uuid4()}.invalid'))
            row.status = 'invalid_upload'
            row.updated_at = datetime.utcnow()
            db.commit()
            raise HTTPException(422, '文件校验和不一致，异常上传已隔离保留；点击重试或重选原文件重新上传')
        validate_media(file)
        row.status = 'ready'
        row.updated_at = datetime.utcnow()
        db.commit()
    return {'asset': asset_out(row), 'reused': False}


@contextmanager
def finalize_slot():
    if not FINALIZE_SLOTS.acquire(blocking=False):
        raise HTTPException(429, '文件校验正在排队，已上传部分保留，请稍后重试')
    try:
        yield
    finally:
        FINALIZE_SLOTS.release()


def private_file_response(db: Session, owner: str, asset_id: str, download=False):
    row = owned_asset(db, asset_id, owner)
    if row.status != 'ready' or not media_path(row.id).is_file():
        raise HTTPException(404, '私人素材尚未就绪')
    return FileResponse(media_path(row.id), media_type=row.content_type, filename=row.filename,
                        content_disposition_type='attachment' if download else 'inline',
                        headers={'Cache-Control': 'private, no-store', 'Vary': 'Cookie, X-OA-Token', 'X-Content-Type-Options': 'nosniff'})


def build_private_router(require_user, require_workstation, publish_to_team):
    router = APIRouter()
    upload_slots = asyncio.Semaphore(4)

    @router.get('/api/private-assets')
    def list_private(q: str = '', category: str = '', folder: str = '', trash: bool = False,
                     page: int = Query(1, ge=1), page_size: int = Query(24, ge=1, le=100),
                     db: Session = Depends(get_db), user: dict = Depends(require_user)):
        filters = [PrivateAsset.owner_number == owner_number(user), PrivateAsset.status == 'ready',
                   PrivateAsset.deleted_at.is_not(None) if trash else PrivateAsset.deleted_at.is_(None)]
        if q.strip():
            filters.append(PrivateAsset.filename.contains(q.strip()))
        if category:
            filters.append(PrivateAsset.category == category)
        if folder:
            filters.append(PrivateAsset.folder_name == folder)
        total = db.scalar(select(func.count()).select_from(PrivateAsset).where(*filters)) or 0
        rows = db.scalars(select(PrivateAsset).where(*filters).order_by(PrivateAsset.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
        folders = db.scalars(select(PrivateAsset.folder_name).where(PrivateAsset.owner_number == owner_number(user), PrivateAsset.deleted_at.is_(None)).distinct()).all()
        return {'items': [asset_out(row) for row in rows], 'total': total, 'page': page, 'folders': sorted(value for value in folders if value)}

    @router.post('/api/private-assets/uploads')
    def start(payload: PrivateUploadCreate, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        return create_upload(db, user, payload)

    @router.get('/api/private-assets/{asset_id}/upload')
    def upload_status(asset_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        return asset_out(owned_asset(db, asset_id, owner_number(user)), offset=True)

    @router.put('/api/private-assets/{asset_id}/upload')
    async def upload_part(asset_id: str, request: Request, offset: int = Query(ge=0), db: Session = Depends(get_db), user: dict = Depends(require_user)):
        owner = owner_number(user)
        await run_in_threadpool(owned_asset, db, asset_id, owner)
        async with upload_slots:
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > CHUNK_BYTES:
                    raise HTTPException(413, '单个分片不能超过 8 MiB')
            return await run_in_threadpool(append_chunk, db, owner, asset_id, offset, bytes(content))

    @router.post('/api/private-assets/{asset_id}/complete')
    def complete(asset_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        return complete_upload(db, owner_number(user), asset_id)

    @router.get('/api/private-assets/{asset_id}/media')
    def media(asset_id: str, download: bool = False, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        return private_file_response(db, owner_number(user), asset_id, download)

    @router.patch('/api/private-assets/{asset_id}')
    def metadata(asset_id: str, payload: PrivateMetadata, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        row = owned_asset(db, asset_id, owner_number(user))
        with asset_lock(row.id):
            db.refresh(row)
            if row.deleted_at:
                raise HTTPException(404, '私人素材不存在')
            for key, value in payload.model_dump(exclude_none=True).items():
                if key == 'filename':
                    value = safe_name(value)
                    if Path(value).suffix.lower() != Path(row.filename).suffix.lower():
                        raise HTTPException(400, '重命名不能改变文件格式')
                setattr(row, key, value.strip() or ('待分类' if key == 'category' else ''))
            row.updated_at = datetime.utcnow()
            db.commit()
        return asset_out(row)

    @router.delete('/api/private-assets/{asset_id}')
    def remove(asset_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        row = owned_asset(db, asset_id, owner_number(user), allow_deleted=True)
        with asset_lock(row.id):
            row.deleted_at = row.deleted_at or datetime.utcnow()
            db.commit()
        return {'ok': True, 'recoverable': True}

    @router.post('/api/private-assets/{asset_id}/restore')
    def restore(asset_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        row = owned_asset(db, asset_id, owner_number(user), allow_deleted=True)
        with asset_lock(row.id):
            row.deleted_at = None
            db.commit()
        return asset_out(row)

    @router.post('/api/private-assets/{asset_id}/share')
    def share(asset_id: str, payload: PrivateShare, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        if not payload.confirmed:
            raise HTTPException(400, '请确认将完整原文件复制到团队素材库，团队同事将可以查看和使用')
        row = owned_asset(db, asset_id, owner_number(user))
        with asset_lock(row.id):
            db.refresh(row)
            if row.deleted_at or row.status != 'ready':
                raise HTTPException(409, '素材未就绪')
            return publish_to_team(db, row, media_path(row.id), user)

    @router.get('/api/workstation/private-assets')
    def workstation_list(actor_number: str, q: str = '', page: int = Query(1, ge=1), page_size: int = Query(24, ge=1, le=100),
                         db: Session = Depends(get_db), _service: dict = Depends(require_workstation)):
        return list_private(q=q, page=page, page_size=page_size, db=db, user={'number': actor_number})

    @router.get('/api/workstation/private-assets/{asset_id}')
    def workstation_detail(asset_id: str, actor_number: str, db: Session = Depends(get_db), _service: dict = Depends(require_workstation)):
        row = owned_asset(db, asset_id, owner_number({'number': actor_number}))
        if row.status != 'ready':
            raise HTTPException(404, '私人素材未就绪')
        return asset_out(row)

    @router.get('/api/workstation/private-assets/{asset_id}/media')
    def workstation_media(asset_id: str, actor_number: str, db: Session = Depends(get_db), _service: dict = Depends(require_workstation)):
        return private_file_response(db, owner_number({'number': actor_number}), asset_id)

    return router
