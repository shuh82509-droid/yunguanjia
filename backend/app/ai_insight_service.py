import threading
from datetime import datetime, timedelta

from sqlalchemy import func, select

from .database import SessionLocal
from .models import (
    AdqDelivery,
    Asset,
    ChannelsDelivery,
    ModuleAccessGrant,
    OaAccessGrant,
    OperationLog,
    QianchuanDelivery,
)


MODULE_STATES = (
    {"key": "data-dashboard", "label": "根数据看板", "purpose": "经营情况总览", "state": "unknown"},
    {"key": "creative-hub", "label": "创意中枢平台", "purpose": "创意来源", "state": "unknown"},
    {"key": "ai-first-creation", "label": "AI一创工作台", "purpose": "一创创作", "state": "unknown"},
    {"key": "material-workbench", "label": "WIS素材工作台", "purpose": "二创混剪", "state": "unknown"},
    {"key": "cloud-manager", "label": "云管家项目", "purpose": "素材存储与推送回流", "state": "unknown"},
    {"key": "live-room-management", "label": "直播间", "purpose": "直播间", "state": "unknown"},
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None


def _count(db, model, *filters) -> int:
    statement = select(func.count()).select_from(model)
    if filters:
        statement = statement.where(*filters)
    return int(db.scalar(statement) or 0)


def _status_counts(db, model, column, *filters) -> dict[str, int]:
    statement = select(column, func.count()).select_from(model)
    if filters:
        statement = statement.where(*filters)
    rows = db.execute(statement.group_by(column)).all()
    return {str(status or "unknown"): int(total or 0) for status, total in rows}


class AiInsightService:
    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot: dict | None = None

    def refresh(self) -> dict:
        now = datetime.utcnow()
        day_cutoff = now - timedelta(days=1)
        week_cutoff = now - timedelta(days=7)
        stale_cutoff = now - timedelta(hours=24)
        stuck_cutoff = now - timedelta(hours=2)
        with SessionLocal() as db:
            active_assets = _count(db, Asset, Asset.deleted_at.is_(None), Asset.purged_at.is_(None))
            trash_assets = _count(db, Asset, Asset.deleted_at.is_not(None), Asset.purged_at.is_(None))
            latest_asset_at = db.scalar(select(func.max(Asset.modified_at)).where(Asset.purged_at.is_(None)))

            operations_total = _count(db, OperationLog, OperationLog.created_at >= day_cutoff)
            operations_failed = _count(
                db,
                OperationLog,
                OperationLog.created_at >= day_cutoff,
                OperationLog.result == "failed",
            )
            failed_modules = [
                {"module": str(module or "系统"), "failed": int(total or 0)}
                for module, total in db.execute(
                    select(OperationLog.module, func.count())
                    .where(OperationLog.created_at >= day_cutoff, OperationLog.result == "failed")
                    .group_by(OperationLog.module)
                    .order_by(func.count().desc())
                    .limit(5)
                ).all()
            ]

            qianchuan_status = _status_counts(
                db, QianchuanDelivery, QianchuanDelivery.status, QianchuanDelivery.deleted_at.is_(None)
            )
            qianchuan_metrics = _status_counts(
                db,
                QianchuanDelivery,
                QianchuanDelivery.metrics_link_status,
                QianchuanDelivery.deleted_at.is_(None),
            )
            qianchuan_failed_recent = _count(
                db,
                QianchuanDelivery,
                QianchuanDelivery.deleted_at.is_(None),
                QianchuanDelivery.created_at >= week_cutoff,
                QianchuanDelivery.status == "failed",
            )
            qianchuan_stuck = _count(
                db,
                QianchuanDelivery,
                QianchuanDelivery.deleted_at.is_(None),
                QianchuanDelivery.updated_at < stuck_cutoff,
                QianchuanDelivery.status.in_(("pending", "uploading", "binding", "uploaded")),
            )
            qianchuan_metrics_stale = _count(
                db,
                QianchuanDelivery,
                QianchuanDelivery.deleted_at.is_(None),
                QianchuanDelivery.updated_at < stale_cutoff,
                QianchuanDelivery.metrics_link_status == "pending",
            )

            adq_status = _status_counts(db, AdqDelivery, AdqDelivery.status, AdqDelivery.deleted_at.is_(None))
            adq_metrics = _status_counts(db, AdqDelivery, AdqDelivery.metrics_status, AdqDelivery.deleted_at.is_(None))
            adq_failed_recent = _count(
                db,
                AdqDelivery,
                AdqDelivery.deleted_at.is_(None),
                AdqDelivery.created_at >= week_cutoff,
                AdqDelivery.status == "failed",
            )
            adq_stuck = _count(
                db,
                AdqDelivery,
                AdqDelivery.deleted_at.is_(None),
                AdqDelivery.updated_at < stuck_cutoff,
                AdqDelivery.status.in_(("pending", "uploading", "binding")),
            )

            channels_status = _status_counts(
                db, ChannelsDelivery, ChannelsDelivery.status, ChannelsDelivery.deleted_at.is_(None)
            )
            channels_failed_recent = _count(
                db,
                ChannelsDelivery,
                ChannelsDelivery.deleted_at.is_(None),
                ChannelsDelivery.created_at >= week_cutoff,
                ChannelsDelivery.status == "failed",
            )
            channels_stuck = _count(
                db,
                ChannelsDelivery,
                ChannelsDelivery.deleted_at.is_(None),
                ChannelsDelivery.updated_at < stale_cutoff,
                ChannelsDelivery.status.in_(("pending", "publishing", "submitted")),
            )

            active_grants = db.scalars(select(OaAccessGrant).where(OaAccessGrant.active.is_(True))).all()
            missing_department = sum(not str(item.department or "").strip() for item in active_grants)
            missing_center = sum(not str(item.center or "").strip() for item in active_grants)
            scoped_grants = _count(db, ModuleAccessGrant, ModuleAccessGrant.access_mode == "selected")

        failure_rate = operations_failed / operations_total if operations_total else 0.0
        insights: list[dict] = []

        def add(
            key: str,
            severity: str,
            category: str,
            title: str,
            summary: str,
            evidence: list[str],
            suggestion: str,
            module_key: str,
            prompt: str,
            admin_only: bool = False,
        ) -> None:
            insights.append({
                "key": key,
                "severity": severity,
                "category": category,
                "title": title,
                "summary": summary,
                "evidence": evidence,
                "suggestion": suggestion,
                "module_key": module_key,
                "prompt": prompt,
                "admin_only": admin_only,
            })

        if operations_failed >= 10 and failure_rate >= 0.1:
            top_module = failed_modules[0] if failed_modules else {"module": "系统", "failed": operations_failed}
            add(
                "operation-failure-rate",
                "high" if failure_rate >= 0.25 else "medium",
                "运行稳定性",
                "近 24 小时操作失败比例偏高",
                "失败请求已达到需要排查的水平，建议先确认是否集中在同一功能或同一调用方式。",
                [
                    f"近 24 小时失败 {operations_failed}/{operations_total}（{failure_rate:.1%}）",
                    f"失败最集中模块：{top_module['module']}（{top_module['failed']} 次）",
                ],
                "先核对集中失败模块的输入校验、客户端版本和接口调用参数，再决定是否修复。",
                "cloud-manager",
                "请结合近24小时操作日志，分析失败比例偏高的原因、核验顺序和低风险修复方案。",
            )

        if qianchuan_failed_recent or qianchuan_stuck:
            add(
                "qianchuan-delivery-attention",
                "high" if qianchuan_stuck >= 10 else "medium",
                "素材推送",
                "千川推送存在近期失败或长时间未完成任务",
                "推送链路里仍有需要复核的任务，不能把未完成或失败直接视为素材无效。",
                [f"近 7 天失败 {qianchuan_failed_recent} 条", f"超过 2 小时未完成 {qianchuan_stuck} 条"],
                "按上传、绑定、计划关联和平台回读四个阶段逐项核验，保留待核验状态。",
                "cloud-manager",
                "请分析千川推送近期失败和长时间未完成任务，给出分阶段核验步骤，不要把缺失数据当成0。",
            )

        if qianchuan_metrics_stale:
            add(
                "qianchuan-metrics-stale",
                "medium",
                "数据回流",
                "部分千川任务超过 24 小时仍待回流",
                "任务已经进入回流等待，但尚未形成可验证结果。",
                [f"超过 24 小时仍为 pending：{qianchuan_metrics_stale} 条"],
                "核对视频 ID、计划绑定、日级任务执行和平台返回状态，未回流继续标记待核验。",
                "cloud-manager",
                "请根据当前千川回流状态，分析超过24小时仍待回流的可能原因和排查顺序。",
            )

        if adq_failed_recent or adq_stuck:
            add(
                "adq-delivery-attention",
                "medium",
                "素材推送",
                "腾讯 ADQ 推送存在待处理任务",
                "近期失败或长时间未完成任务需要复核账户、营销单元和素材绑定状态。",
                [f"近 7 天失败 {adq_failed_recent} 条", f"超过 2 小时未完成 {adq_stuck} 条"],
                "按授权、素材入库、营销单元绑定和数据回流顺序核验。",
                "cloud-manager",
                "请分析腾讯ADQ推送待处理任务，给出授权、入库、绑定和回流的核验步骤。",
            )

        if channels_failed_recent or channels_stuck:
            add(
                "channels-delivery-attention",
                "medium",
                "视频号推送",
                "视频号发布链路存在待处理任务",
                "近期失败或长时间未完成的发布记录需要核对授权、页面提交和结果回读。",
                [f"近 7 天失败 {channels_failed_recent} 条", f"超过 24 小时未完成 {channels_stuck} 条"],
                "先核对授权是否过期，再检查发布提交和平台内容 ID 回读。",
                "cloud-manager",
                "请分析视频号发布链路待处理任务，给出授权、提交和回读三个阶段的排查方案。",
            )

        if missing_department or missing_center:
            add(
                "permission-profile-completeness",
                "low",
                "权限质量",
                "部分授权成员的部门或中心信息仍待补全",
                "组织字段缺失不会扩大权限，但会影响权限检索和批量管理效率。",
                [f"缺少部门 {missing_department} 人", f"缺少中心 {missing_center} 人"],
                "成员下次 OA 登录时自动回填；管理员可按缺失字段持续复核。",
                "hub",
                "请分析权限成员组织字段缺失对管理效率的影响，并给出不扩大权限的补全方案。",
                admin_only=True,
            )

        snapshot = {
            "scanned_at": _iso(now),
            "sources": [ "素材库状态", "操作日志", "推送与回流", "统一权限"],
            "modules": {"online": None, "total": len(MODULE_STATES), "state": "unverified", "reason": "当前巡检未执行跨模块业务可用性验证"},
            "assets": {"active": active_assets, "trash": trash_assets, "latest_at": _iso(latest_asset_at)},
            "operations_24h": {
                "total": operations_total,
                "failed": operations_failed,
                "failure_rate": round(failure_rate, 4) if operations_total else None,
                "failed_modules": failed_modules,
            },
            "delivery": {
                "qianchuan": {"status": qianchuan_status, "metrics": qianchuan_metrics},
                "adq": {"status": adq_status, "metrics": adq_metrics},
                "channels": {"status": channels_status},
            },
            "permissions": {
                "active": len(active_grants),
                "missing_department": missing_department,
                "missing_center": missing_center,
                "scoped": scoped_grants,
            },
            "insights": insights,
        }
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _current(self) -> dict:
        with self._lock:
            snapshot = self._snapshot
        return snapshot or self.refresh()

    def status(self) -> dict:
        with self._lock:
            snapshot = self._snapshot
        return {
            "status": "ready" if snapshot else "starting",
            "scanned_at": snapshot.get("scanned_at") if snapshot else None,
            "interval_mode": "scheduled_read_only",
        }

    def view(self, allowed_modules: list[str], can_manage_permissions: bool = False) -> dict:
        snapshot = self._current()
        allowed = set(allowed_modules)
        visible_insights = [
            {key: value for key, value in item.items() if key != "admin_only"}
            for item in snapshot["insights"]
            if (not item["admin_only"] or can_manage_permissions)
            and (item["module_key"] == "hub" or item["module_key"] in allowed)
        ]
        severity_counts = {
            severity: sum(item["severity"] == severity for item in visible_insights)
            for severity in ("high", "medium", "low")
        }
        scanned = datetime.fromisoformat(snapshot["scanned_at"].removesuffix("Z"))
        stale = datetime.utcnow() - scanned > timedelta(minutes=30)
        visible_snapshot = {
            "stale": stale,
            "scanned_at": snapshot["scanned_at"],
            "sources": snapshot["sources"],
            "modules": snapshot["modules"],
        }
        if "cloud-manager" in allowed:
            visible_snapshot.update({
                "assets": snapshot["assets"],
                "operations_24h": snapshot["operations_24h"],
                "delivery": snapshot["delivery"],
            })
        if can_manage_permissions:
            visible_snapshot["permissions"] = snapshot["permissions"]
        return {
            "status": "stale" if stale else "attention" if severity_counts["high"] or severity_counts["medium"] else "stable",
            "summary": {"total": len(visible_insights), **severity_counts},
            "snapshot": visible_snapshot,
            "insights": visible_insights,
        }


ai_insight_service = AiInsightService()
