import zipfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import backup_tools


class BackupToolsTests(unittest.TestCase):
    def test_backup_round_trip_keeps_existing_files_and_omits_key(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "ai_workspace_data"
            data.mkdir()
            (data / "settings.json").write_text('{"theme":"dark"}', encoding="utf-8")
            key = root / "key.env"
            key.write_text("TOKEN=secret", encoding="utf-8")
            archive = root / "backup.zip"
            backup_tools.create_backup(data, archive, key_file=key)
            (data / "extra.txt").write_text("keep", encoding="utf-8")
            (data / "settings.json").write_text("{}", encoding="utf-8")
            safety = backup_tools.restore_backup(archive, data, key_file=key)
            self.assertEqual((data / "settings.json").read_text(encoding="utf-8"), '{"theme":"dark"}')
            self.assertEqual((data / "extra.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual(key.read_text(encoding="utf-8"), "TOKEN=secret")
            self.assertTrue(safety and (safety / "extra.txt").exists())
            with zipfile.ZipFile(archive) as zf:
                self.assertNotIn("secrets/key.env", zf.namelist())

    def test_restore_rejects_path_traversal(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("ai_workspace_data/../../outside.txt", "bad")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                backup_tools.restore_backup(archive, Path(tmp) / "ai_workspace_data")
