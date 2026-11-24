from queue import Queue
from unittest.mock import Mock

from plugins.cerry_jobs_1 import Hunter, LETTER_SYSTEM


def test_selected_job_uses_saved_posting_without_a_web_request(tmp_path, monkeypatch):
    hunter = Hunter.__new__(Hunter)
    hunter.stop_event = Mock()
    hunter.q = Queue()
    hunter.log = Mock()
    hunter.ensure_profile = Mock(return_value=({}, "CV"))
    hunter.offline = Mock(return_value=True)
    hunter.description_for = Mock(side_effect=AssertionError("unexpected network lookup"))
    hunter.process_job = Mock()
    rec = dict(id="job1", title="Engineer", employer="Example", location="Berlin", source="board",
               url="https://example.com/job", folder=str(tmp_path), analysis={}, status="saved")
    hunter.find = Mock(return_value=rec)
    desc = "Project description. " * 25
    (tmp_path / "job.md").write_text("# Job\n\n## Posting text\n\n" + desc)
    monkeypatch.setattr("plugins.cerry_jobs_1.Web", Mock())
    hunter.worker("__letter__", rec["id"])
    hunter.process_job.assert_called_once_with(rec, desc.strip(), {}, {}, "CV", documents="letter")
    hunter.description_for.assert_not_called()


def test_letter_only_checks_claims_and_preserves_existing_cv(tmp_path):
    hunter = Hunter.__new__(Hunter)
    hunter.cfg = {"formats": [], "tailor": False}
    hunter.log = hunter.progress = hunter.upsert = Mock()
    hunter.llm = Mock(side_effect=["Draft letter", "Corrected letter"])
    hunter.llm_json = Mock(side_effect=[{"letter": ["unsupported claim"]}, {"letter": []}])
    cv = tmp_path / "markdown" / "CV_tailored.md"
    cv.parent.mkdir()
    cv.write_text("Existing CV")
    rec = dict(title="Engineer", employer="Example", location="Berlin", letter_focus="analysis")
    hunter.tailor(rec, "Job text", {"top_keywords": []}, {}, "Original CV", tmp_path, "English", letter_only=True)
    assert cv.read_text() == "Existing CV"
    assert hunter.llm.call_args_list[0].args[0] == LETTER_SYSTEM
    assert "analysis" in hunter.llm.call_args_list[0].args[1]
    assert hunter.llm_json.call_count == 2
    assert (cv.parent / "CoverLetter.md").read_text() == "Corrected letter"
    assert "unsupported claim" in (tmp_path / "cover_letter_check.md").read_text()
