"""Personal target selections. Loading a scheme never queues a platform task."""
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Integer, JSON, String, DateTime, select, update, delete
from sqlalchemy.orm import Mapped, mapped_column, Session

from .database import Base


class PushScheme(Base):
    __tablename__ = "push_schemes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(80))
    targets: Mapped[list] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SchemeTarget(BaseModel):
    advertiser_id: str = Field(min_length=1, max_length=80, pattern=r"^[0-9]+$")
    advertiser_name: str = Field(default="", max_length=255)
    plan_id: str = Field(min_length=1, max_length=80, pattern=r"^[0-9]+$")
    plan_name: str = Field(default="", max_length=255)
    plan_type: str = Field(pattern=r"^(multiplication|full_domain|standard)$")


class SchemeInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    targets: list[SchemeTarget] = Field(min_length=1, max_length=50)
    revision: int = Field(default=0, ge=0)


def output(row):
    return {"id": row.id, "name": row.name, "targets": row.targets,
            "revision": row.revision, "updated_at": row.updated_at.isoformat() + "Z"}


def install_push_schemes(app, get_db, require_user, user_number):
    router = APIRouter(prefix="/api/push-schemes/qianchuan")

    @router.get("")
    def listing(db: Session = Depends(get_db), user: dict = Depends(require_user)):
        rows = db.scalars(select(PushScheme).where(PushScheme.owner_number == user_number(user))
                          .order_by(PushScheme.updated_at.desc())).all()
        return {"items": [output(row) for row in rows]}

    def data(payload):
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "请输入方案名称")
        targets = [t.model_dump() for t in payload.targets]
        keys = {(t["advertiser_id"], t["plan_type"], t["plan_id"]) for t in targets}
        if len(keys) != len(targets):
            raise HTTPException(422, "方案包含重复计划")
        return name, targets

    @router.post("")
    def create(payload: SchemeInput, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        name, targets = data(payload)
        row = PushScheme(id=str(uuid4()), owner_number=user_number(user), name=name, targets=targets)
        db.add(row)
        db.commit()
        db.refresh(row)
        return output(row)

    @router.put("/{scheme_id}")
    def replace(scheme_id: str, payload: SchemeInput, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        name, targets = data(payload)
        owner = user_number(user)
        row = db.scalar(select(PushScheme).where(PushScheme.id == scheme_id, PushScheme.owner_number == owner))
        if row is None:
            raise HTTPException(404, "方案不存在")
        changed = db.execute(update(PushScheme).where(PushScheme.id == scheme_id, PushScheme.owner_number == owner,
                              PushScheme.revision == payload.revision).values(name=name, targets=targets,
                              revision=payload.revision + 1, updated_at=datetime.utcnow()))
        if changed.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "方案已被更新，请重新读取后再保存")
        db.commit()
        db.refresh(row)
        return output(row)

    @router.delete("/{scheme_id}")
    def remove(scheme_id: str, revision: int, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        owner = user_number(user)
        row = db.scalar(select(PushScheme).where(PushScheme.id == scheme_id, PushScheme.owner_number == owner))
        if row is None:
            raise HTTPException(404, "方案不存在")
        changed = db.execute(delete(PushScheme).where(PushScheme.id == scheme_id, PushScheme.owner_number == owner,
                                                     PushScheme.revision == revision))
        if changed.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "方案已被更新，请重新读取")
        db.commit()
        return {"deleted": True}

    app.include_router(router)
