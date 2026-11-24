import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from realtime_voice import GeminiLiveVoice


def test_primary_screen_is_encoded_in_memory():
    import io
    source = Image.new("RGB", (2000, 1000))
    with patch("PIL.ImageGrab.grab", return_value=source) as grab:
        frame = GeminiLiveVoice._screen_jpeg()
    grab.assert_called_once_with(all_screens=False)
    with Image.open(io.BytesIO(frame)) as image:
        assert image.format == "JPEG"
        assert image.size == (1600, 800)


def test_sends_one_video_frame_and_stops():
    live = GeminiLiveVoice("test", "", Mock(), share_screen=True)
    session = SimpleNamespace(send_realtime_input=AsyncMock(side_effect=lambda **_: live.stop()))
    with patch.object(live, "_screen_jpeg", return_value=b"frame"), patch("realtime_voice.asyncio.sleep", new_callable=AsyncMock):
        asyncio.run(live._stream_screen(session, SimpleNamespace(Blob=dict)))
    session.send_realtime_input.assert_awaited_once_with(video={"data": b"frame", "mime_type": "image/jpeg"})


def test_stop_during_capture_discards_frame():
    live = GeminiLiveVoice("test", "", Mock(), share_screen=True)
    session = SimpleNamespace(send_realtime_input=AsyncMock())
    with patch.object(live, "_screen_jpeg", side_effect=live.stop):
        asyncio.run(live._stream_screen(session, SimpleNamespace(Blob=dict)))
    session.send_realtime_input.assert_not_awaited()
