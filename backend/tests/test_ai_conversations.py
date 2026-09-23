import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.database import Base, SessionLocal, engine
from app.main import (
    AssistantAttachmentCreate,
    AssistantChatPayload,
    AssistantConversationCreate,
    assistant_attachment_create,
    assistant_chat,
    assistant_conversation_create,
    assistant_conversation_messages,
    assistant_conversations,
)
from app.models import AssistantAttachment, AssistantConversation, AssistantConversationMessage


class AiConversationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)

    def setUp(self):
        with SessionLocal() as db:
            db.query(AssistantAttachment).delete()
            db.query(AssistantConversationMessage).delete()
            db.query(AssistantConversation).delete()
            db.commit()

    @staticmethod
    def user(number: str, name: str) -> dict:
        return {"number": number, "realName": name, "groupName": "品牌营销部", "status": "normal"}

    def test_conversations_are_private_to_the_oa_owner(self):
        owner = self.user("FD-AI-1", "会话甲")
        other = self.user("FD-AI-2", "会话乙")
        with SessionLocal() as db:
            conversation = assistant_conversation_create(AssistantConversationCreate(title="我的业务分析"), db, owner)
            self.assertEqual(assistant_conversations(db=db, user=owner)["total"], 1)
            self.assertEqual(assistant_conversations(db=db, user=other)["total"], 0)
            with self.assertRaises(HTTPException) as raised:
                assistant_conversation_messages(conversation["id"], db, other)
            self.assertEqual(raised.exception.status_code, 404)

    def test_image_upload_and_chat_persist_user_and_assistant_messages(self):
        owner = self.user("FD-AI-3", "图片用户")
        gif = base64.b64encode(base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")).decode("ascii")
        with tempfile.TemporaryDirectory() as temp_dir, SessionLocal() as db:
            conversation = assistant_conversation_create(AssistantConversationCreate(), db, owner)
            fake_settings = SimpleNamespace(
                ai_assistant_upload_dir=temp_dir,
                ai_assistant_image_max_bytes=8 * 1024 * 1024,
            )
            with patch("app.main.settings", fake_settings):
                attachment = assistant_attachment_create(AssistantAttachmentCreate(
                    conversation_id=conversation["id"],
                    filename="test.gif",
                    mime_type="image/gif",
                    data_base64=gif,
                ), db, owner)
            self.assertTrue(any(Path(temp_dir).rglob("*.gif")))

            model_result = {
                "answer": "我能看到一张测试图片。",
                "actions": [],
                "meta": {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp", "mode": "read_only", "latency_ms": 12},
            }
            with patch("app.main.module_access_for_user", return_value={"allowed_modules": []}), \
                 patch("app.main._assistant_context", return_value={"sources": []}), \
                 patch("app.main.ai_assistant_service.chat", return_value=model_result) as chat:
                result = assistant_chat(AssistantChatPayload(
                    conversation_id=conversation["id"],
                    content="请分析图片",
                    attachment_ids=[attachment["id"]],
                ), owner, db)

            self.assertEqual(result["conversation"]["message_count"], 2)
            self.assertEqual(result["user_message"]["attachments"][0]["filename"], "test.gif")
            self.assertEqual(result["assistant_message"]["content"], "我能看到一张测试图片。")
            model_messages = chat.call_args.args[0]
            self.assertIsInstance(model_messages[-1]["content"], list)
            history = assistant_conversation_messages(conversation["id"], db, owner)
            self.assertEqual([item["role"] for item in history["items"]], ["user", "assistant"])


if __name__ == "__main__":
    unittest.main()
