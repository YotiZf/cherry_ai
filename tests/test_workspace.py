import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import workspace


class WorkspaceTests(unittest.TestCase):
    def test_chat_round_trip_and_workspace_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace.configure(Path(tmp) / "workspaces")
            alpha = workspace.Workspace("alpha")
            chat = alpha.new_chat("Test chat")
            chat["messages"].append({"role": "user", "content": "hello"})
            alpha.save_chat(chat)
            self.assertEqual(alpha.last_chat_id(), chat["id"])
            self.assertEqual(alpha.load_chat(chat["id"])["messages"][0]["content"], "hello")
            beta = workspace.Workspace("beta")
            with self.assertRaises(FileNotFoundError):
                beta.load_chat(chat["id"])
