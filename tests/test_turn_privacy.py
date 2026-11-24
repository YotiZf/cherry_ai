import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "plugins")]
import cerry_local_agent
import cerry_memory
import routing
from turn_context import TurnContext


class TurnPrivacyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.a, self.b = [SimpleNamespace(name=n, notes_path=root / n / "notes.md") for n in ("A", "B")]
        for ws in (self.a, self.b):
            ws.notes_path.parent.mkdir()
            ws.notes_path.write_text("- project " + ws.name)
        global_path = root / "global.md"
        global_path.write_text("- personal preference")
        self.app = Mock(ws=self.a, chat={"id": "chat-a"}, policy=routing.RoutingPolicy(local_only=True))
        self.app.document_source_var.get.return_value = "Workspace documents"
        self.app.use_global.get.return_value = False
        self.app.use_workspace_memory.get.return_value = self.app.use_ws.get.return_value = True
        models = [{"label": "cloud", "provider": "gemini"}, {"label": "local", "provider": "ollama"}]
        self.app.selected_model.return_value = models[0]
        self.stream = Mock()
        self.stream.run_chat.return_value = {"answer": json.dumps([
            {"scope": "global", "text": "Always answer in English"},
            {"scope": "workspace", "text": "Write the report in German"}])}
        self.mem = cerry_memory.Memory(self.app, dict(WS_DIR=root, GLOBAL_MEMORY=global_path,
            MODELS=models, DATA_DIR=root, streaming=self.stream, routing=routing, get_client=Mock()))

    def test_extraction_uses_original_turn_and_filters_returned_scopes(self):
        turn = TurnContext.capture(self.app, "Use German for this report")
        self.app.ws = self.b
        self.app.policy.local_only = False
        self.app.use_global.get.return_value = True
        self.mem.after_reply({"content": "Understood"}, turn, force=True)
        kind, workspace, items = self.mem.q.get(timeout=3)
        self.assertEqual((kind, workspace), ("items", "A"))
        self.assertEqual([i["scope"] for i in items], ["workspace"])
        order, messages = self.stream.run_chat.call_args.args[:2]
        self.assertEqual([m["label"] for m in order], ["local"])
        prompt = messages[1]["content"]
        self.assertIn("project A", prompt)
        self.assertIn(turn.user_text, prompt)
        self.assertNotIn("project B", prompt)
        self.assertNotIn("personal preference", prompt)

    def test_no_extraction_when_memory_is_disabled_or_documents_only(self):
        for mode, workspace_memory in (("Workspace documents", False), ("Only use chat documents", True)):
            self.app.document_source_var.get.return_value = mode
            self.app.use_workspace_memory.get.return_value = workspace_memory
            turn = TurnContext.capture(self.app, "Please remember the report language")
            self.mem.after_reply({"content": "Understood"}, turn, force=True)
            self.assertFalse(self.mem.working)
            self.stream.run_chat.assert_not_called()

    def test_approve_after_switch_and_restart(self):
        self.mem.add_pending("Write reports in German", "workspace", "A")
        self.app.ws = self.b
        restored = cerry_memory.Memory(self.app, self.mem.ns)
        panel = cerry_memory.Panel(restored)
        panel.sug_selected = lambda: restored.pending[0]
        panel.sug_keep()
        self.assertIn("Write reports in German", self.a.notes_path.read_text())
        self.assertNotIn("Write reports in German", self.b.notes_path.read_text())
        self.assertEqual(restored.pending, [])

    def test_local_only_rejects_external_source_before_reading_files(self):
        agent = Mock(app=self.app, busy=False)
        agent.task_var.get.return_value = "Update this project"
        for source in cerry_local_agent.SOURCES[1:]:
            agent.provider_var.get.return_value = source
            with patch.object(cerry_local_agent.engine, "gather") as gather:
                cerry_local_agent.LocalAgent.start(agent)
                gather.assert_not_called()
                agent.run_cli.assert_not_called()
