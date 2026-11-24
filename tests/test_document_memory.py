import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import doc_manager
import workspace


class DocumentMemoryTests(unittest.TestCase):
    def test_in_memory_search_keeps_tie_order_and_rebuilds_postings(self):
        index = doc_manager.DocumentIndex()
        self.a.write_text("shared term")
        self.b.write_text("shared term")
        sources = self.ws.sources()
        index.build(sources)
        hits = index.search("shared missing")
        self.assertEqual([hit["file"] for hit in hits], ["alpha.txt", "beta.txt"])
        self.assertEqual(hits[0]["score"], hits[1]["score"])
        self.assertEqual(index.search("shared", excluded={hits[0]["key"]}), hits[1:])
        self.a.unlink()
        index.build(sources)
        self.assertEqual([hit["file"] for hit in index.search("shared")], ["beta.txt"])

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace.configure(Path(tmp.name))
        self.ws = workspace.Workspace("test")
        self.a = self.ws.docs / "alpha.txt"
        self.b = self.ws.docs / "beta.txt"
        self.a.write_text("A bicycle has wheels.")
        self.b.write_text("Bicycle repair. Bicycle parts.")
        self.ws.reindex()

    def test_reopen_and_incremental_updates(self):
        ws = workspace.Workspace("test")
        search = ws.index.search
        self.assertEqual(search("bicycle in alpha.txt")[0]["file"], "alpha.txt")
        self.assertTrue(search("summarize alpha.txt"))
        self.assertEqual(search("wheels", excluded={"documents/alpha.txt"}), [])
        with patch.object(doc_manager, "extract_pages", wraps=doc_manager.extract_pages) as read:
            read.__module__ = "doc_manager"
            read.cache_revision = lambda p: None
            ws.reindex()
            read.assert_not_called()
            self.a.write_text("New battery information")
            self.b.unlink()
            ws.reindex()
            read.assert_called_once_with(self.a)
        self.assertTrue(search("battery"))
        self.assertFalse(search("bicycle"))

    def test_reviews_cover_every_section_and_reuse_by_instructions_and_version(self):
        self.a.write_text("a"*12000+"final exception")
        self.ws.reindex()
        memory = self.ws.index.memory
        ask = Mock(return_value="findings")
        key = "documents/alpha.txt"
        title, result = memory.review(key, "review", ask, lambda s: None)
        self.assertIn("2 of 2", result)
        self.assertIn("final exception", ask.call_args_list[1].args[1])
        self.assertEqual(workspace.Workspace("test").index.memory.review(key,"review",None,None)[1], result)
        memory.review(key, "other instruction", ask, lambda s: None)
        self.assertEqual(ask.call_count, 6)
        self.a.write_text("Different content")
        with self.assertRaises(ValueError):
            memory.review(key, "review", ask, lambda s: None)
        self.ws.reindex()
        progress = []
        memory.review(key, "review", ask, progress.append)
        self.assertIn("outdated", progress[0])
