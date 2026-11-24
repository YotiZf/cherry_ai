"""Gemini Live audio-to-audio voice session used by Cherry's optional real-time mode."""
import asyncio
import io
import queue
import re
import threading


class GeminiLiveVoice:
    """Stream microphone PCM to Gemini Live and play native audio replies."""
    MODEL = "gemini-3.8-live"
    INPUT_RATE = 16000
    OUTPUT_RATE = 24000
    FRAMES = 1600                 # 100 ms of mono PCM16

    def __init__(self, api_key, system_instruction, on_event, voice_name="Aoede", share_screen=False):
        self.api_key = api_key
        self.system_instruction = system_instruction
        self.on_event = on_event
        self.voice_name = voice_name
        self.share_screen = share_screen
        self._stop = threading.Event()
        self._audio = queue.Queue(maxsize=48)
        self._loop = None
        self._task = None
        self._thread = None
        self._play_thread = None

    def start(self):
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name="cherry-gemini-live")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._loop and self._task:
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                pass

    def _emit(self, kind, value=None):
        try:
            self.on_event(kind, value)
        except Exception:
            pass

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as exc:
            if not self._stop.is_set():
                self._emit("error", str(exc)[:300])
        finally:
            self._stop.set()
            self._emit("ended")

    async def _run(self):
        try:
            import sounddevice as sd
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("Real-time voice needs google-genai and sounddevice. Install the updated requirements.") from exc

        client = genai.Client(api_key=self.api_key, http_options={"api_version": "v1beta"})
        config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": self.system_instruction,
            "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": self.voice_name}}},
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "realtime_input_config": {
                "automatic_activity_detection": {"disabled": False, "silence_duration_ms": 650}
            },
        }
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        if self._stop.is_set():
            return
        if self.share_screen:
            config["context_window_compression"] = {"sliding_window": {}}
        self._play_thread = threading.Thread(target=self._play_audio, args=(sd,), daemon=True,
                                             name="cherry-live-playback")
        self._play_thread.start()
        try:
            async with client.aio.live.connect(model=self.MODEL, config=config) as session:
                self._emit("ready")
                inputs = [asyncio.create_task(self._capture(session, sd, types))]
                if self.share_screen:
                    inputs.append(asyncio.create_task(self._stream_screen(session, types)))
                try:
                    input_text = ""
                    while not self._stop.is_set():
                        # The SDK receive iterator ends after a completed turn; reopen it
                        # on the same session so the next spoken turn is received too.
                        async for response in session.receive():
                            if self._stop.is_set():
                                break
                            content = getattr(response, "server_content", None)
                            if content is None:
                                continue
                            incoming = getattr(content, "input_transcription", None)
                            if incoming and getattr(incoming, "text", None):
                                text = incoming.text.strip()
                                input_text = (input_text + " " + text).strip()
                                normalized = re.sub(r"[^\w\s]", " ", input_text.casefold())
                                normalized = " ".join(normalized.split())
                                exits = ("end the conversation", "end conversation", "stop conversation",
                                         "stop talking", "goodbye", "bye", "stop", "stopp",
                                         "gespräch beenden", "unterhaltung beenden", "tschüss")
                                if any(normalized == phrase or normalized.endswith(" " + phrase)
                                       for phrase in exits):
                                    self._emit("end_requested")
                                    break
                                self._emit("user_text", text)
                            outgoing = getattr(content, "output_transcription", None)
                            if outgoing and getattr(outgoing, "text", None):
                                self._emit("assistant_text", outgoing.text)
                            turn = getattr(content, "model_turn", None)
                            if turn:
                                for part in turn.parts or []:
                                    inline = getattr(part, "inline_data", None)
                                    data = getattr(inline, "data", None) if inline else None
                                    if data:
                                        try:
                                            self._audio.put_nowait(data)
                                        except queue.Full:
                                            self._emit("warning", "Audio playback fell behind; dropping an old audio chunk.")
                            if getattr(content, "interrupted", False):
                                self._clear_audio()
                                self._emit("interrupted")
                            if getattr(content, "turn_complete", False):
                                self._emit("turn_complete")
                                input_text = ""
                        if not self._stop.is_set():
                            continue
                finally:
                    for task in inputs:
                        task.cancel()
                    await asyncio.gather(*inputs, return_exceptions=True)
        finally:
            self._stop.set()
            self._clear_audio()

    @staticmethod
    def _screen_jpeg():
        from PIL import ImageGrab
        frame = ImageGrab.grab(all_screens=False)
        frame.thumbnail((1600, 1200))
        with io.BytesIO() as output:
            frame.convert("RGB").save(output, format="JPEG", quality=82)
            return output.getvalue()

    async def _stream_screen(self, session, types):
        try:
            while not self._stop.is_set():
                frame = await asyncio.to_thread(self._screen_jpeg)
                if self._stop.is_set():
                    return
                await session.send_realtime_input(video=types.Blob(data=frame, mime_type="image/jpeg"))
                await asyncio.sleep(1)
        except Exception as exc:
            self._emit("error", f"Screen sharing failed: {str(exc)[:240]}")
            self.stop()

    async def _capture(self, session, sd, types):
        try:
            with sd.InputStream(samplerate=self.INPUT_RATE, channels=1, dtype="int16",
                                blocksize=self.FRAMES) as stream:
                while not self._stop.is_set():
                    data, _overflow = await asyncio.to_thread(stream.read, self.FRAMES)
                    await session.send_realtime_input(audio=types.Blob(
                        data=data.tobytes(), mime_type=f"audio/pcm;rate={self.INPUT_RATE}"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit("error", f"Microphone failed: {str(exc)[:240]}")
            if self._task:
                self._task.cancel()
            raise

    def _play_audio(self, sd):
        import numpy as np
        try:
            with sd.OutputStream(samplerate=self.OUTPUT_RATE, channels=1, dtype="int16") as stream:
                while not self._stop.is_set():
                    try:
                        chunk = self._audio.get(timeout=0.15)
                    except queue.Empty:
                        continue
                    if chunk:
                        stream.write(np.frombuffer(chunk, dtype=np.int16))
        except Exception as exc:
            if not self._stop.is_set():
                self._emit("error", f"Audio playback failed: {str(exc)[:240]}")
                self._stop.set()
                if self._loop and self._task:
                    self._loop.call_soon_threadsafe(self._task.cancel)

    def _clear_audio(self):
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                return
