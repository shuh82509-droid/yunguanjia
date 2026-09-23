import json
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.long_term_work_service import MAX_AI_EVIDENCE_CHARS, LongTermWorkError, LongTermWorkService


SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_service(tmp_path):
    seed = {
        "schemaVersion": 1,
        "generatedAt": "2026-08-26T10:15:00+08:00",
        "sourceMode": "verified-snapshot",
        "sourceChat": {"name": "品牌营销部-核心干将", "chatId": "chat", "readThroughDate": "2026-08-26"},
        "definition": "test",
        "items": [{
            "id": "old", "title": "原事项", "center": "ALL", "owners": ["舒豪"],
            "startDate": "2026-08-25", "endDate": "2026-09-01", "status": "进行中",
            "acceptance": "完成验收", "sourceType": "飞书文档", "sourceTitle": "文档",
            "sourceUrl": "https://example.com/docx/old",
        }],
    }
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return LongTermWorkService(tmp_path / "state.json", seed_path)


def test_success_advances_cursor_and_updates_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_list_messages", lambda *_: [{"body": {"content": json.dumps({"text": "新增长期事项"})}}])
    monkeypatch.setattr(service, "_known_document_urls", lambda *_: [])
    monkeypatch.setattr(service, "_extract", lambda *_: [{
        "id": "new", "title": "新增事项", "center": "ALL", "owners": ["舒豪"],
        "startDate": "2026-08-27", "endDate": "2026-09-30", "status": "进行中",
        "acceptance": "按月验收", "sourceType": "核心干将群", "sourceTitle": "核心干将群",
        "sourceUrl": "https://applink.feishu.cn/client/chat/open?openChatId=chat",
    }])

    result = service.run(force=True, now=datetime(2026, 8, 27, 10, 20, tzinfo=SHANGHAI))

    assert result["status"] == "ready"
    assert result["lastSuccessfulDate"] == "2026-08-27"
    assert result["messagesRead"] == 1
    assert service.snapshot()["items"][0]["id"] == "new"


def test_permission_failure_preserves_last_valid_snapshot_and_cursor(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        service,
        "_list_messages",
        lambda *_: (_ for _ in ()).throw(LongTermWorkError("机器人未入群", "blocked_permission")),
    )

    result = service.run(force=True, now=datetime(2026, 8, 27, 10, 20, tzinfo=SHANGHAI))

    assert result["status"] == "blocked_permission"
    assert result["lastReadThroughTime"] is None
    assert service.snapshot()["items"][0]["id"] == "old"


def test_due_run_does_not_repeat_after_same_day_success(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    state = service._read_state()
    state["lastSuccessfulDate"] = "2026-08-27"
    state["status"] = "ready"
    service._write_state(state)
    called = False

    def fail_if_called(*_):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(service, "_list_messages", fail_if_called)
    result = service.run(force=False, now=datetime(2026, 8, 27, 11, 0, tzinfo=SHANGHAI))

    assert result["status"] == "ready"
    assert called is False


def test_due_run_does_not_repeat_same_day_permission_failure(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = 0

    def blocked(*_):
        nonlocal calls
        calls += 1
        raise LongTermWorkError("机器人未入群", "blocked_permission")

    monkeypatch.setattr(service, "_list_messages", blocked)
    now = datetime(2026, 8, 27, 10, 20, tzinfo=SHANGHAI)
    assert service.run(force=False, now=now)["status"] == "blocked_permission"
    assert service.run(force=False, now=now.replace(hour=12))["status"] == "blocked_permission"
    assert calls == 1


def test_disabled_schedule_has_no_next_run(tmp_path):
    service = make_service(tmp_path)
    status = service.configure(enabled=False, hour=10, minute=15)
    assert status["status"] == "disabled"
    assert status["nextRunAt"] is None


def test_inaccessible_document_is_skipped_without_blocking_refresh(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_list_messages", lambda *_: [{"body": {"content": json.dumps({"text": "群消息仍可用"})}}])
    monkeypatch.setattr(service, "_known_document_urls", lambda *_: ["https://example.feishu.cn/docx/missing"])
    monkeypatch.setattr(
        service,
        "_document_content",
        lambda *_: (_ for _ in ()).throw(LongTermWorkError("飞书读取失败：not found，已保留最近成功快照")),
    )
    monkeypatch.setattr(service, "_extract", lambda *_: [{
        "id": "from-chat", "title": "群内长期事项", "center": "ALL", "owners": ["舒豪"],
        "startDate": "2026-08-28", "endDate": "2026-09-30", "status": "进行中",
        "acceptance": "按月验收", "sourceType": "核心干将群", "sourceTitle": "核心干将群",
        "sourceUrl": "https://applink.feishu.cn/client/chat/open?openChatId=chat",
    }])

    result = service.run(force=True, now=datetime(2026, 8, 28, 10, 20, tzinfo=SHANGHAI))

    assert result["status"] == "ready"
    assert result["messagesRead"] == 1
    assert result["documentsRead"] == 0
    assert result["documentErrorCount"] == 1
    assert result["documentErrors"][0]["url"].endswith("/docx/missing")
    assert service.snapshot()["items"][0]["id"] == "from-chat"


def test_compact_evidence_keeps_action_signals_and_caps_document_payload(tmp_path):
    service = make_service(tmp_path)
    messages = [
        {"text": "大家早上好"},
        {"text": "每周二完成素材复盘，负责人彭聪，9月30日前持续执行并按结果验收"},
    ]
    documents = [
        {
            "title": "月度计划",
            "url": "https://example.com/docx/plan",
            "content": "无关背景" * 5000 + "负责人舒豪，下周完成AI认证并形成验收记录" + "补充背景" * 5000,
        }
    ]

    evidence = service._compact_evidence(
        messages,
        documents,
        service.snapshot()["items"],
        datetime(2026, 8, 28, tzinfo=SHANGHAI).date(),
    )

    assert evidence["evidenceStats"]["rawMessageCount"] == 2
    assert evidence["evidenceStats"]["selectedMessageCount"] == 1
    assert evidence["messages"][0]["sourceType"] == "核心干将群"
    assert evidence["messages"][0]["sourceUrl"].startswith("https://applink.feishu.cn/client/chat/open")
    assert "每周二完成素材复盘" in evidence["messages"][0]["text"]
    assert evidence["evidenceStats"]["selectedDocumentChars"] <= 2200
    assert "负责人舒豪" in evidence["documents"][0]["content"]
    assert len(json.dumps(evidence, ensure_ascii=False)) < 10000


def test_evidence_batches_keep_each_ai_request_bounded_and_previous_only_once(tmp_path):
    service = make_service(tmp_path)
    messages = [
        {"text": f"第{index}条每周计划，负责人舒豪，月底完成并验收。" + "补充说明" * 300}
        for index in range(80)
    ]
    documents = [{
        "title": "长期计划",
        "url": "https://example.com/docx/long",
        "content": "每周持续推进，负责人舒豪，9月30日前完成并验收。" * 500,
    }]
    evidence = service._compact_evidence(
        messages,
        documents,
        service.snapshot()["items"],
        datetime(2026, 8, 28, tzinfo=SHANGHAI).date(),
    )

    batches = service._evidence_batches(evidence)

    assert len(batches) > 1
    assert all(len(json.dumps(batch, ensure_ascii=False)) <= MAX_AI_EVIDENCE_CHARS for batch in batches)
    assert batches[0]["previousItems"]
    assert all(not batch["previousItems"] for batch in batches[1:])
    assert sum(len(batch["messages"]) for batch in batches) == len(evidence["messages"])
    assert sum(len(batch["documents"]) for batch in batches) == len(evidence["documents"])


def test_failed_ai_batch_is_split_and_each_child_is_recovered(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    def fake_post(*_, **kwargs):
        evidence = json.loads(kwargs["json"]["requests_data"]["messages"][1]["content"])
        source_count = sum(len(evidence[key]) for key in ("previousItems", "messages", "documents"))
        calls.append(source_count)
        if source_count > 1:
            return FakeResponse(504)
        return FakeResponse(200, {"choices": [{"message": {"content": "[]"}}]})

    monkeypatch.setattr("backend.app.long_term_work_service.requests.post", fake_post)
    batch = {
        "today": "2026-08-28",
        "previousItems": [],
        "messages": [{"text": "每周完成复盘"}, {"text": "负责人舒豪月底验收"}],
        "documents": [],
        "evidenceStats": {},
    }

    assert service._request_evidence_batch(batch, "prompt", "1/1") == []
    assert calls == [2, 1, 1]


def test_terminal_single_evidence_failure_is_deferred_with_source(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    class FakeResponse:
        status_code = 504

    monkeypatch.setattr("backend.app.long_term_work_service.requests.post", lambda *_, **__: FakeResponse())
    monkeypatch.setattr("backend.app.long_term_work_service.time.sleep", lambda *_: None)
    batch = {
        "today": "2026-08-28",
        "previousItems": [],
        "messages": [{
            "text": "每周完成复盘",
            "sourceUrl": "https://applink.feishu.cn/client/chat/open?openChatId=chat",
        }],
        "documents": [],
        "evidenceStats": {},
    }

    assert service._request_evidence_batch(batch, "prompt", "9/9") == []
    assert service._last_ai_errors[0]["label"] == "9/9.retry"
    assert service._last_ai_errors[0]["sourceUrls"] == [
        "https://applink.feishu.cn/client/chat/open?openChatId=chat"
    ]


def test_extract_deterministically_preserves_unexpired_previous_items(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    import importlib
    from dataclasses import replace

    module = importlib.import_module("backend.app.long_term_work_service")
    monkeypatch.setattr(module, "settings", replace(module.settings, jump_llm_token="configured"))
    monkeypatch.setattr(service, "_request_evidence_batch", lambda *_: [])

    previous = service.snapshot()["items"]
    result = service._extract([], [], previous, datetime(2026, 8, 28, tzinfo=SHANGHAI).date())

    assert result == previous
