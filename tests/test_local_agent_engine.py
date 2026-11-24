import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import local_agent_engine as agent


class LocalAgentTests(unittest.TestCase):
    def test_discovery_prunes_generated_trees_and_preserves_path_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in ("a/nested.py", "NODE_MODULES/dependency.js", "a.py", ".github/work.yml"):
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.touch()
            (root / "large.bin").write_bytes(b"x" * (agent.MAX_FILE_BYTES + 1))
            with patch("file_scan.os.scandir", wraps=agent.os.scandir) as scan:
                _, files, skipped = agent.gather(root)
            self.assertNotIn(root / "NODE_MODULES", [Path(call.args[0]) for call in scan.call_args_list])
            self.assertEqual([f["path"] for f in files], [".github/work.yml", "a/nested.py", "a.py"])
            self.assertEqual(skipped, ["large.bin (size limit)"])

    def test_gather_skips_secrets_and_generated_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("print('hi')", encoding="utf-8")
            (root / ".env").write_text("SECRET=x", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "config.pem").write_text("key", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("internal", encoding="utf-8")
            _, files, _ = agent.gather(root)
            self.assertEqual([item["path"] for item in files], ["main.py"])

    def test_proposal_rejects_parent_traversal_and_protected_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path in ("../outside.py", ".env"):
                raw = json.dumps({"changes": [{"path": path, "action": "write", "content": "x"}]})
                with self.assertRaises(ValueError):
                    agent.parse_proposal(raw, tmp, [])

    def test_apply_makes_backup_and_rejects_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            target = root / "main.py"
            target.write_text("old", encoding="utf-8")
            _, files, _ = agent.gather(root)
            proposal = agent.parse_proposal('{"changes":[{"path":"main.py","action":"write","content":"new"}]}', root, files)
            backup = agent.apply_changes(root, proposal["changes"], Path(tmp) / "backups")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual((backup / "main.py").read_text(encoding="utf-8"), "old")

            _, files, _ = agent.gather(root)
            proposal = agent.parse_proposal('{"changes":[{"path":"main.py","action":"delete"}]}', root, files)
            target.write_text("changed outside agent", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed since"):
                agent.apply_changes(root, proposal["changes"], Path(tmp) / "backups")
