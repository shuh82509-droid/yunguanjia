"""Explicit, audited business roles. Never changes administrative privileges."""
from datetime import datetime
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .admission_policy import confirmed_departure
from .database import Base, SessionLocal, engine, get_db
from .models import ModuleAccessGrant, OaAccessAuditLog, OaAccessGrant


CENTERS = ("AI营销中心", "视频中心", "营销中心A", "营销中心B", "营销中心C", "营销中心D", "营销中心J", "品牌创意中心", "品牌营销中心", "直播中心", "未分中心")
SCOPES = {"director": "department", "manager": "center", "specialist": "personal"}


class WorkspaceRoleGrant(Base):
    __tablename__ = "workspace_role_grants"
    identifier: Mapped[str] = mapped_column(String(180), primary_key=True)
    role: Mapped[str] = mapped_column(String(24))
    center: Mapped[str] = mapped_column(String(120))
    department: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by_name: Mapped[str] = mapped_column(String(120))
    updated_at: Mapped[str] = mapped_column(Text)
    __mapper_args__ = {"version_id_col": version}


class WorkspaceRoleUpdate(BaseModel):
    role: Literal["director", "manager", "specialist"]
    center: str
    modules: list[str] = Field(max_length=30)
    version: int = Field(ge=0)


def configured_profile(user: dict, db: Session | None = None) -> dict | None:
    number = str(user.get("number") or user.get("userId") or user.get("id") or "").strip().upper()
    name = str(user.get("realName") or user.get("name") or "").strip()
    identifiers = ([f"number:{number}"] if number else []) + ([f"name:{name.casefold()}"] if name else [])
    owned = db is None
    session = db or SessionLocal()
    try:
        for identifier in identifiers:
            profile = session.get(WorkspaceRoleGrant, identifier)
            if not profile:
                continue
            grant = session.get(OaAccessGrant, identifier)
            evidence = " ".join(str(user.get(k) or "") for k in ("department", "parentDept", "deptName", "groupName"))
            if not grant or not grant.active or "品牌营销部" not in evidence or (grant.user_number and grant.user_number.upper() != number):
                raise HTTPException(403, "角色配置与当前有效身份不一致，请联系权限管理员核对")
            return {"role": profile.role, "center": profile.center, "department": profile.department,
                    "dashboard_scope": SCOPES[profile.role], "version": profile.version,
                    "configured": True, "source": "workspace-role-config"}
        return None
    finally:
        if owned:
            session.close()


def configured_scope(user: dict) -> dict | None:
    profile = configured_profile(user)
    if not profile:
        return None
    return {"scope": profile["dashboard_scope"], "department": profile["department"],
            "centers": list(CENTERS) if profile["role"] == "director" else [profile["center"]] if profile["role"] == "manager" else [],
            "personName": str(user.get("realName") or user.get("name") or "")}


def apply_member_profiles(db: Session, members: list[dict]) -> list[dict]:
    rows = db.execute(select(WorkspaceRoleGrant, OaAccessGrant).join(
        OaAccessGrant, WorkspaceRoleGrant.identifier == OaAccessGrant.identifier
    ).where(OaAccessGrant.active.is_(True))).all()
    for profile, grant in rows:
        for member in members:
            same = member.get("employeeId") == grant.user_number if grant.user_number else member.get("personName") == grant.real_name
            if same and "品牌营销部" in grant.department:
                member.update(center=profile.center, department=profile.department,
                              isLeader=profile.role != "specialist",
                              leaderCenters=[profile.center] if profile.role == "manager" else [])
    return members


def install_workspace_roles(app, require_user, require_manager, module_catalog):
    # Additive table only; existing grants, uploads and task tables are untouched.
    WorkspaceRoleGrant.__table__.create(engine, checkfirst=True)
    module_keys = {item["key"] for item in module_catalog}

    def item_for(db, grant):
        from .main import is_permission_manager
        profile = db.get(WorkspaceRoleGrant, grant.identifier)
        modules = db.get(ModuleAccessGrant, grant.identifier)
        selected = list(modules.modules or []) if modules and modules.access_mode == "selected" else list(module_keys)
        departed = confirmed_departure({"number": grant.user_number or grant.identifier}) is not None
        highest = not departed and is_permission_manager({"number": grant.user_number or grant.identifier, "realName": grant.real_name})
        return {"identifier": grant.identifier, "real_name": grant.real_name, "user_number": grant.user_number,
                "department": grant.department, "center": profile.center if profile else grant.center,
                "oa_center": grant.center, "login_active": bool(grant.active) and not departed,
                "login_blocked": departed, "login_blocked_reason": "已确认离职" if departed else "",
                "role": profile.role if profile else None, "modules": selected,
                "configured": bool(profile), "version": profile.version if profile else 0,
                "module_configured": modules is not None,
                "module_access_mode": modules.access_mode if modules else "all",
                "highest_business_access": highest,
                "effective_modules": [] if departed else list(module_keys) if highest else selected,
                "editable": bool(not departed and grant.active and "品牌营销部" in grant.department),
                "updated_by_name": profile.updated_by_name if profile else "",
                "updated_at": profile.updated_at if profile else None}

    @app.get("/api/admin/workspace-profiles")
    def list_profiles(q: str = "", db: Session = Depends(get_db), user: dict = Depends(require_user)):
        require_manager(user)
        rows = db.scalars(select(OaAccessGrant).order_by(OaAccessGrant.real_name, OaAccessGrant.identifier)).all()
        query = q.strip().casefold()
        items = [item_for(db, row) for row in rows]
        if query:
            items = [item for item in items if query in " ".join(str(item.get(k) or "") for k in ("real_name", "user_number", "department", "center")).casefold()]
        return {"items": items, "total": len(items), "centers": list(CENTERS), "catalog": list(module_catalog)}

    @app.put("/api/admin/workspace-profiles/{identifier:path}")
    def save_profile(identifier: str, payload: WorkspaceRoleUpdate, db: Session = Depends(get_db), user: dict = Depends(require_user)):
        from sqlalchemy.orm.exc import StaleDataError
        from .main import is_permission_manager
        require_manager(user)
        grant = db.get(OaAccessGrant, identifier)
        if not grant:
            raise HTTPException(404, "请先在成员准入中添加并核对该同事")
        if not grant.active or confirmed_departure({"number": grant.user_number or grant.identifier}):
            raise HTTPException(409, "该同事的登录权限已关闭；保存角色不会恢复准入")
        if is_permission_manager({"number": grant.user_number or grant.identifier, "realName": grant.real_name}):
            raise HTTPException(409, "最高权限统一使用部门视角；请先取消最高权限，再修改普通角色与模块")
        if "品牌营销部" not in grant.department:
            raise HTTPException(409, "请先核对成员的品牌营销部归属；部门外例外授权保持原规则")
        duplicates = db.scalars(select(OaAccessGrant).where(OaAccessGrant.real_name == grant.real_name, OaAccessGrant.active.is_(True))).all()
        if len(duplicates) > 1:
            raise HTTPException(409, "存在同名或重复授权记录，请先核对工号并消除歧义")
        if payload.center not in CENTERS or (payload.role != "director" and payload.center == "未分中心"):
            raise HTTPException(400, "请选择有效中心；主管和专员必须明确所属中心")
        if set(payload.modules) - module_keys:
            raise HTTPException(400, "包含未识别的业务模块")
        row = db.get(WorkspaceRoleGrant, identifier)
        if payload.version != (row.version if row else 0):
            raise HTTPException(409, "配置已被其他操作更新，请刷新后再保存")
        actor = str(user.get("realName") or user.get("name") or "")
        actor_number = str(user.get("number") or "")
        before = {"role": row.role, "center": row.center} if row else {"source": "existing-mapping"}
        if not row:
            row = WorkspaceRoleGrant(identifier=identifier)
            db.add(row)
        row.role, row.center, row.department = payload.role, payload.center, "品牌营销部"
        row.updated_by_name, row.updated_at = actor, datetime.utcnow().isoformat() + "Z"
        modules = db.get(ModuleAccessGrant, identifier)
        if not modules:
            modules = ModuleAccessGrant(identifier=identifier, identifier_type=grant.identifier_type)
            db.add(modules)
        modules.real_name, modules.user_number = grant.real_name, grant.user_number
        modules.department, modules.center = grant.department, payload.center
        modules.access_mode = "selected"
        modules.modules = [item["key"] for item in module_catalog if item["key"] in set(payload.modules)]
        modules.updated_by_name, modules.updated_by_number, modules.updated_at = actor, actor_number, datetime.utcnow()
        db.add(OaAccessAuditLog(identifier=identifier, real_name=grant.real_name, action="workspace_role",
                               actor_number=actor_number, actor_name=actor,
                               detail=f"业务角色配置：{before} → {payload.role}/{payload.center}；模块：{','.join(modules.modules)}；未修改管理员身份"))
        try:
            db.commit()
        except StaleDataError:
            db.rollback()
            raise HTTPException(409, "配置已被其他操作更新，请刷新后再保存")
        return item_for(db, grant)
