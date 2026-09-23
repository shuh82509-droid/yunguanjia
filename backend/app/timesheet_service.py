import json
import os
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

import requests


SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
DEFAULT_BASE_TOKEN = "D94qbjMFIa8wPqsEo5NcMWYNnHd"
DEFAULT_TABLE_ID = "tbl2RSC3JOEFLXd4"
SOURCE_URL = "https://jqx28l0j4lx.feishu.cn/base/D94qbjMFIa8wPqsEo5NcMWYNnHd"
SOURCE_TITLE = "工时分析 · 【AA】工时原始数据-月度-每日13点之后更新"
ALLOWED_JOB_CATEGORIES = {"部门负责人", "岛民", "主管、见习经理、中心负责人"}


class TimesheetError(RuntimeError):
    pass


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_text(item) for item in value]
        return "、".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "name", "value", "full_name"):
            if key in value:
                return _text(value.get(key))
        return "、".join(part for part in (_text(item) for item in value.values()) if part)
    return str(value).strip()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list) and len(value) == 1:
        return _number(value[0])
    if isinstance(value, dict):
        for key in ("value", "number"):
            if key in value:
                return _number(value.get(key))
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _month_label(day: date) -> str:
    return f"{day.year}年{day.month}月"


def _normalize_name(value: str) -> str:
    return "".join(str(value or "").split()).casefold()


class TimesheetService:
    """Read the governed Feishu Base timesheet table and retain the last valid snapshot."""

    def __init__(self) -> None:
        self.app_id = os.getenv("FEISHU_APP_ID", "").strip()
        self.app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        self.base_token = os.getenv("FEISHU_TIMESHEET_BASE_TOKEN", DEFAULT_BASE_TOKEN).strip() or DEFAULT_BASE_TOKEN
        self.table_id = os.getenv("FEISHU_TIMESHEET_TABLE_ID", DEFAULT_TABLE_ID).strip() or DEFAULT_TABLE_ID
        self.snapshot_path = Path(os.getenv("FEISHU_TIMESHEET_SNAPSHOT_PATH", "/data/timesheet_snapshot.json"))
        self.bundled_snapshot_paths = (
            Path(__file__).parent / "data" / "timesheet_snapshot_20260828.json",
            Path(__file__).parent / "data" / "timesheet_snapshot.json",
        )
        self.cache_seconds = max(300, int(os.getenv("FEISHU_TIMESHEET_CACHE_SECONDS", "900")))
        self._token = ""
        self._token_expires_at = 0.0
        self._cache: dict[str, tuple[float, dict]] = {}
        self._cache_files: dict[str, tuple] = {}
        self._failures: dict[str, tuple[float, str]] = {}
        self.failure_cache_seconds = 60
        self._lock = Lock()

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret and self.base_token and self.table_id)

    def _tenant_access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token
        response = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("tenant_access_token") or "")
        if payload.get("code") not in (0, None) or not token:
            raise TimesheetError(str(payload.get("msg") or "飞书应用令牌获取失败"))
        self._token = token
        self._token_expires_at = now + max(60, int(payload.get("expire") or 7200) - 120)
        return token

    def _live_records(self, month: str) -> tuple[list[dict], int | None]:
        if not self.configured:
            raise TimesheetError("飞书工时应用凭证未配置")
        token = self._tenant_access_token()
        page_token = ""
        records: list[dict] = []
        revision: int | None = None
        while True:
            params: dict[str, Any] = {
                "page_size": 500,
                "filter": f'CurrentValue.[所属月份]="{month}"',
            }
            if page_token:
                params["page_token"] = page_token
            response = requests.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.base_token}/tables/{self.table_id}/records",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=30,
            )
            payload = response.json()
            if response.status_code >= 400 or payload.get("code") not in (0, None):
                code = payload.get("code")
                message = str(payload.get("msg") or f"HTTP {response.status_code}")[:500]
                if code == 99991672:
                    raise TimesheetError(f"飞书工时应用缺少读取权限（code {code}）：{message}")
                raise TimesheetError(f"飞书工时读取失败（HTTP {response.status_code}，code {code}）：{message}")
            data = payload.get("data") or {}
            records.extend(data.get("items") or [])
            revision = data.get("revision") or data.get("rev") or revision
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise TimesheetError("飞书工时分页标记缺失")
        return records, int(revision) if revision is not None else None

    @staticmethod
    def _parse_records(records: list[dict], month: str) -> list[dict]:
        members: list[dict] = []
        for record in records:
            fields = record.get("fields") or {}
            # Verify returned facts as well as the upstream query. A changed or
            # ignored filter must never label an August row as September.
            if _text(fields.get("所属月份")) != month:
                continue
            department = _text(fields.get("当月工时归属部门"))
            if "品牌营销部" not in department:
                continue
            if _text(fields.get("当月岗位二级分类")) not in ALLOWED_JOB_CATEGORIES:
                continue
            if _text(fields.get("当月所属部门分类")) != "营销":
                continue
            if any(_text(fields.get(field)) != "统计" for field in ("工时统计（排除异常工时）", "公司工时统计", "部门工时统计")):
                continue
            average_hours = _number(fields.get("平均工作时长-新"))
            if average_hours is None or not (5 < average_hours < 17):
                continue
            name = _text(fields.get("姓名"))
            employee_id = _text(fields.get("工号-新")) or _text(fields.get("工号"))
            if not name:
                continue
            members.append({
                "personName": name,
                "employeeId": employee_id,
                "department": "品牌营销部",
                "center": _text(fields.get("当月工时归属中心")) or "未分中心",
                "monthLabel": month,
                "averageEffectiveHours": round(average_hours, 2),
                "totalEffectiveHours": _number(fields.get("总工作时长-新")),
                "scheduledDays": _number(fields.get("排班天数")),
                "punchDays": _number(fields.get("打卡天数")),
                "averageAttendanceHours": _number(fields.get("平均出勤时长")),
            })
        members.sort(key=lambda item: (item["center"], item["personName"]))
        return members

    @staticmethod
    def _summary(members: list[dict]) -> dict:
        def average(field: str) -> float | None:
            values = [float(item[field]) for item in members if item.get(field) is not None]
            return round(sum(values) / len(values), 2) if values else None

        return {
            "memberCount": len(members),
            "averageEffectiveHours": average("averageEffectiveHours"),
            "averageAttendanceHours": average("averageAttendanceHours"),
            "averageScheduledDays": average("scheduledDays"),
            "averagePunchDays": average("punchDays"),
            "totalEffectiveHours": round(sum(float(item.get("totalEffectiveHours") or 0) for item in members), 2) if members else None,
        }

    def _snapshot_document(self, members: list[dict], month: str, *, revision: int | None, source_mode: str, state: str, note: str) -> dict:
        return {
            "schemaVersion": 1,
            "generatedAt": datetime.now(SHANGHAI).isoformat(),
            "monthLabel": month,
            "state": state,
            "sourceMode": source_mode,
            "source": {
                "title": SOURCE_TITLE,
                "url": SOURCE_URL,
                "baseToken": self.base_token,
                "tableId": self.table_id,
                "revision": revision,
                "note": note,
                "definition": "与品牌营销部工时仪表盘口径一致：平均工作时长-新扣除午休、有薪加班与17:30-18:30晚餐重叠，排除实习生，并应用公司与异常工时统计规则。",
            },
            "summary": self._summary(members),
            "members": members,
        }

    def _read_snapshot(self, month: str) -> dict:
        for path in (self.snapshot_path, *self.bundled_snapshot_paths):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("monthLabel") == month and isinstance(payload.get("members"), list):
                return payload
        raise TimesheetError(f"{month} 工时数据待回补")

    def _persist_snapshot(self, payload: dict) -> None:
        try:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.snapshot_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.snapshot_path)
        except OSError:
            return

    def _snapshot_files(self) -> tuple:
        """External verified user collection must invalidate an older cached view."""
        files = []
        for path in (self.snapshot_path, *self.bundled_snapshot_paths):
            try:
                stat = path.stat()
                files.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                files.append((str(path), None, None))
        return tuple(files)

    def get_snapshot(self, selected_date: date, force: bool = False) -> dict:
        month = _month_label(selected_date)
        now = time.time()
        files = self._snapshot_files()
        cached = self._cache.get(month)
        ttl = self.failure_cache_seconds if cached and cached[1].get("state") == "stale" else self.cache_seconds
        if cached and not force and now - cached[0] < ttl and self._cache_files.get(month) == files:
            return cached[1]
        with self._lock:
            now = time.time()
            files = self._snapshot_files()
            cached = self._cache.get(month)
            ttl = self.failure_cache_seconds if cached and cached[1].get("state") == "stale" else self.cache_seconds
            if cached and not force and now - cached[0] < ttl and self._cache_files.get(month) == files:
                return cached[1]
            live_error = ""
            try:
                failure = self._failures.get(month)
                if failure and not force and now - failure[0] < self.failure_cache_seconds:
                    raise TimesheetError(failure[1])
                records, revision = self._live_records(month)
                members = self._parse_records(records, month)
                if not members:
                    raise TimesheetError(f"飞书工时表未返回 {month} 品牌营销部有效记录")
                payload = self._snapshot_document(
                    members,
                    month,
                    revision=revision,
                    source_mode="live",
                    state="ready",
                    note="飞书 Base 实时读取；源表每日13点后更新。",
                )
                self._persist_snapshot(payload)
                self._failures.pop(month, None)
            except (requests.RequestException, TimesheetError, ValueError) as error:
                live_error = str(error)
                # Preserve the first failure time: repeated reads may reuse this
                # error for one minute, but must not extend the retry deadline.
                previous = self._failures.get(month)
                if not previous or force or now - previous[0] >= self.failure_cache_seconds:
                    self._failures[month] = (time.time(), live_error)
                try:
                    payload = self._read_snapshot(month)
                except TimesheetError as snapshot_error:
                    raise TimesheetError(f"{live_error}；{snapshot_error}") from error
                payload = json.loads(json.dumps(payload, ensure_ascii=False))
                payload["state"] = "stale"
                payload["sourceMode"] = "verified-snapshot"
                payload.setdefault("source", {})["note"] = f"展示最近一次已核验快照；实时同步待恢复：{live_error}"
            self._cache[month] = (time.time(), payload)
            self._cache_files[month] = self._snapshot_files()
            return payload

    @staticmethod
    def index_members(payload: dict) -> tuple[dict[str, dict], dict[str, dict]]:
        by_employee = {
            str(item.get("employeeId") or "").strip().upper(): item
            for item in payload.get("members") or []
            if str(item.get("employeeId") or "").strip()
        }
        by_name = {
            _normalize_name(item.get("personName", "")): item
            for item in payload.get("members") or []
            if str(item.get("personName") or "").strip()
        }
        return by_employee, by_name


timesheet_service = TimesheetService()
