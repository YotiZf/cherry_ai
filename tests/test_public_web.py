import unittest
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from public_web import read_prefix


class ResponseReadTests(unittest.TestCase):
    def test_stops_at_exact_limit_without_reading_another_chunk(self):
        def chunks():
            yield b"ab"
            yield b"cd"
            self.fail("read beyond the configured limit")

        response = Mock()
        response.iter_bytes.return_value = chunks()
        self.assertEqual(read_prefix(response, 4), b"abcd")

    def test_truncates_oversized_chunk_and_preserves_short_responses(self):
        response = Mock()
        response.iter_bytes.return_value = [b"ab", b"cdef"]
        self.assertEqual(read_prefix(response, 3), b"abc")
        self.assertEqual(read_prefix(response, 20), b"abcdef")
