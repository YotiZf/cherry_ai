import subprocess
import queue
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import audio_engine
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
import cerry_audio


class AudioRecoveryTests(unittest.TestCase):
    def test_end_keeps_recognized_phrases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clip.wav"
            clip.touch()
            job = cerry_audio.Job(1, clip, "small", "en", False, "auto",
                                  SimpleNamespace(dir=root, docs=root / "documents"))
            jobs = cerry_audio.AudioQueue(queue.Queue())

            def interrupted(*args, **kwargs):
                args[8]({"start": 1, "text": "first phrase"})
                job.keep_transcript = True
                jobs.stop(job)
                raise audio_engine.Stopped()

            with patch.object(audio_engine, "analyze", side_effect=interrupted):
                jobs._run(job)
            self.assertEqual(job.status, "Done")
            self.assertIn("Partial audio transcript", job.out.read_text(encoding="utf-8"))
            self.assertIn("first phrase", job.text)

    def test_full_transcript_available_before_speakers_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clip.wav"
            clip.touch()
            job = cerry_audio.Job(1, clip, "small", "en", True, "auto",
                                  SimpleNamespace(dir=root, docs=root / "documents"))
            jobs = cerry_audio.AudioQueue(queue.Queue(), lambda: "test")

            def stage(request, callbacks, stop):
                if request["stage"] == "Transcription":
                    return dict(segs=[{"start": 0, "text": "finished speech"}], duration=1,
                                model="small", language="en", probability=1)
                self.assertIn("finished speech", job.out.read_text(encoding="utf-8"))
                job.keep_transcript = True
                raise audio_engine.Stopped()

            with patch.object(audio_engine, "_run_stage", side_effect=stage):
                jobs._run(job)
            self.assertEqual(job.status, "Done")
            self.assertEqual(len(list(job.ws.docs.iterdir())), 1)

    def test_worker_exits_then_speaker_failure_preserves_retry_transcript(self):
        # Exercise real pipe shutdown and abnormal exits without loading a speech model.
        program = """
import json, os, sys
r = json.load(sys.stdin)
if r['stage'] != 'Transcription' or not r['conservative']:
    os._exit(19)
assert not r['words']
print(json.dumps(['result', {'segs': [{'start': 0, 'text': 'hello'}]}]))
"""
        popen = subprocess.Popen
        with patch.object(audio_engine.subprocess, "Popen",
                          side_effect=lambda args, **opts: popen([sys.executable, "-c", program], **opts)):
            result = audio_engine.analyze("example.wav", label_speakers=True, token="test")
        self.assertEqual(result["lines"], ["[00:00:00] hello"])
        self.assertIn("Speaker labels skipped", result["warning"])

    def test_cancel_interrupts_worker_during_native_call(self):
        popen = subprocess.Popen
        with patch.object(audio_engine.subprocess, "Popen", side_effect=lambda args, **opts:
                          popen([sys.executable, "-c", "import time; time.sleep(60)"], **opts)):
            with self.assertRaises(audio_engine.Stopped):
                audio_engine.analyze("example.wav", should_stop=lambda: True)
