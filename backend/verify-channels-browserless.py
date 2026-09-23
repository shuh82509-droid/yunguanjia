"""Release checks; offline clone migration and read-only production verification."""
import json
import sqlite3
import sys
from pathlib import Path

TABLES = ["assets", "channels_accounts", "channels_deliveries", "asset_review_decisions", "oa_access_grants",
          "admin_grants", "module_access_grants", "review_workflow_config", "workspace_role_grants"]
FIELDS = {"channel_cookies_ciphertext", "session_cookie_ciphertext", "cookies_updated_at", "last_verified_at"}
PUSH_TABLES = ['qianchuan_deliveries', 'adq_deliveries']


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def compare(current, baseline, *, exact):
    current.execute("ATTACH DATABASE ? AS prior", (str(baseline),))
    counts = {}
    for table in TABLES + PUSH_TABLES:
        info = current.execute(f'PRAGMA prior.table_info("{table}")').fetchall()
        if not info:
            continue
        columns = [x[1] for x in info if exact or x[5] or table in TABLES[4:]]
        fields = ",".join('"' + x.replace('"', '""') + '"' for x in columns)
        assert not current.execute(f'SELECT {fields} FROM prior."{table}" EXCEPT SELECT {fields} FROM main."{table}" LIMIT 1').fetchone(), f"data_preservation_failed:{table}"
        counts[table] = current.execute(f'SELECT count(*) FROM main."{table}"').fetchone()[0]
    current.execute("DETACH DATABASE prior")
    return counts


mode = sys.argv[1]
if mode == "probe":
    with ro("/data/wis_video_center.db") as db:
        counts = {}
        for table in ("channels_deliveries", "qianchuan_deliveries", "adq_deliveries"):
            counts[table] = dict(db.execute(f"SELECT status,count(*) FROM {table} GROUP BY status"))
        print(json.dumps(counts))
elif mode == "migrate":
    # No network; no production write mount; no browser credentials mounted.
    baseline = Path("/data/pre-migration.db")
    candidate = Path("/data/candidate.db")
    with ro("/source/wis_video_center.db") as source, sqlite3.connect(baseline) as target:
        source.backup(target)
    with ro(baseline) as source, sqlite3.connect(candidate) as target:
        source.backup(target)
    from app.database import Base, engine
    from app.main import ensure_asset_schema
    Base.metadata.create_all(engine)
    ensure_asset_schema()
    with ro(candidate) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert FIELDS <= {x[1] for x in db.execute("PRAGMA table_info(channels_accounts)")}
        counts = compare(db, baseline, exact=True)
        print(json.dumps({"migration": "pass", "preserved_rows": counts}))
elif mode == "post":
    from app.config import settings
    from app.main import app
    assert settings.channels_publish_transport == "auto"
    assert settings.channels_internal_api_browser_fallback is True
    assert not settings.channels_direct_account_ids
    assert "/api/admin/workspace-profiles" in {r.path for r in app.routes}
    with ro("/data/wis_video_center.db") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert FIELDS <= {x[1] for x in db.execute("PRAGMA table_info(channels_accounts)")}
        counts = compare(db, sys.argv[2], exact=False)
    print(json.dumps({"production": "pass", "transport": "auto", "direct_allowlist": [], "browser_fallback": True, "preserved_rows": counts}))
else:
    raise SystemExit("unknown mode")
