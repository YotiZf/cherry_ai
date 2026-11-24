import collections
import io
import json
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
import wave
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from tkinter import ttk

# ----------------------------------------------------------------------
# SETTINGS
# ----------------------------------------------------------------------
@dataclass
class VoiceSettings:
    # listening
    stt_engine: str = "groq"                 # "groq" or "local"
    stt_language: str = "auto"               # "auto", "en", "de"
    groq_model: str = "whisper-large-v3-turbo"
    whisper_model: str = "small"             # local: tiny, base, small, medium (multilingual)
    stt_hint: str = "Cerry, RNA-seq, splicing, transcriptomics"
    # speaking
    en_voice: str = "en_US-lessac-medium"
    de_voice: str = "de_DE-thorsten-medium"
    speed: float = 1.0                       # 1.0 normal, >1 faster
    auto_read: bool = False
    default_lang: str = "en"
    # conversation mode
    silence_ms: int = 900                    # pause that ends your turn
    vad_sensitivity: float = 1.0             # >1 reacts to quieter speech
    idle_timeout_s: int = 90                 # end conversation after this much silence (0 = never)
    max_utterance_s: int = 30
    interrupt: bool = False                  # speaking over Cerry stops it (use headphones)

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            names = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in names})
        except Exception:
            return cls()

    def save(self, path):
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# ----------------------------------------------------------------------
# TEXT PREPARATION AND LANGUAGE DETECTION
# ----------------------------------------------------------------------
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_CITE = re.compile(r"\[(?:D?\d+(?:\s*,\s*D?\d+)*)\]")

_DE_WORDS = set("der die das und ist nicht ich sie wir ein eine einen mit für auf von den dem des "
                "zu im es aber auch wie wenn oder noch nur bei aus nach über dass kann wird sind "
                "haben hat ihr sein werden sich".split())
_EN_WORDS = set("the and is are of to that it for with as this be not on you have but or at from "
                "can will your they their which".split())

_lingua = {"tried": False, "det": None}


def clean_for_speech(text):
    text = _CODE_BLOCK.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _CITE.sub("", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.M)
    text = re.sub(r"[*_~|>]+", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _heuristic_lang(text):
    low = text.lower()
    words = re.findall(r"[a-zäöüß]+", low)
    de = sum(w in _DE_WORDS for w in words) + 3 * len(re.findall(r"[äöüß]", low))
    en = sum(w in _EN_WORDS for w in words)
    if de == en:
        return None
    return "de" if de > en else "en"


def _lingua_detector():
    if not _lingua["tried"]:
        _lingua["tried"] = True
        try:
            from lingua import Language, LanguageDetectorBuilder
            det = LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.GERMAN).build()
            _lingua["det"] = (det, Language.GERMAN)
        except Exception:
            _lingua["det"] = None
    return _lingua["det"]


def detect_lang(text):
    d = _lingua_detector()
    if d:
        det, german = d
        try:
            lang = det.detect_language_of(text)
        except Exception:
            lang = None
        if lang is not None:
            return "de" if lang == german else "en"
    return _heuristic_lang(text)


class SentenceChunker:
    """Turns streamed text into speakable sentences (skips code blocks, strips markdown)."""
    MIN_CHARS = 25

    def __init__(self, responsive=False):
        self._raw = ""        # text that may still contain a partial ``` fence
        self.buf = ""         # speakable text waiting for a sentence end
        self.carry = ""       # short sentences waiting to be merged
        self.in_code = False
        self.responsive = responsive

    def _strip_fences(self, text):
        self._raw += text
        out = ""
        while True:
            idx = self._raw.find("```")
            if idx < 0:
                keep = 2 if self._raw.endswith("``") else (1 if self._raw.endswith("`") else 0)
                emit = self._raw[:len(self._raw) - keep]
                self._raw = self._raw[len(self._raw) - keep:]
                if not self.in_code:
                    out += emit
                return out
            if not self.in_code:
                out += self._raw[:idx]
            self._raw = self._raw[idx + 3:]
            self.in_code = not self.in_code

    def _take(self, sentences, final=False):
        out = []
        for s in sentences:
            s = clean_for_speech(s)
            if not s:
                continue
            self.carry = (self.carry + " " + s).strip()
            if len(self.carry) >= self.MIN_CHARS:
                out.append(self.carry)
                self.carry = ""
        if final and self.carry:
            out.append(self.carry)
            self.carry = ""
        return out

    def feed(self, text):
        self.buf += self._strip_fences(text)
        parts = re.split(r'(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9"“(\[])|\n+', self.buf)
        if len(parts) <= 1:
            if self.responsive and len(self.buf) >= 60:
                # Speak a natural phrase while the model is still finishing its sentence.
                cut = self.buf.rfind(" ", 35, 61)
                if cut > 0:
                    phrase, self.buf = self.buf[:cut], self.buf[cut + 1:]
                    return self._take([phrase])
            return []
        self.buf = parts[-1]
        return self._take(parts[:-1])

    def flush(self):
        if not self.in_code:
            self.buf += self._raw
        self._raw = ""
        rest, self.buf = self.buf, ""
        return self._take([rest] if rest.strip() else [], final=True)


def split_sentences(text):
    parts = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        pieces = re.split(r'(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9"“(])', line)
        parts += [p.strip() for p in pieces if p.strip()]
    return parts


# ----------------------------------------------------------------------
# TEXT TO SPEECH (Piper, fully local) - supports streaming text
# ----------------------------------------------------------------------
class Speaker:
    def __init__(self, cfg, voices_dir, on_status=None):
        self.cfg = cfg
        self.dir = Path(voices_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.on_status = on_status or (lambda t: None)
        self._voices = {}
        self._voice_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._evt = threading.Event()
        self._text_q = None
        self._stream_id = 0
        self._consumers = 0
        self.last_error = None

    @property
    def speaking(self):
        return self._consumers > 0

    def voice_name(self, lang):
        return self.cfg.de_voice if lang == "de" else self.cfg.en_voice

    def ensure_voice(self, name):
        onnx = self.dir / f"{name}.onnx"
        conf = self.dir / f"{name}.onnx.json"
        if onnx.exists() and conf.exists():
            return onnx
        self.on_status(f"Downloading voice {name} (one time, needs internet)...")
        kwargs = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run([sys.executable, "-m", "piper.download_voices", name],
                           cwd=str(self.dir), capture_output=True, text=True, timeout=900, **kwargs)
        if not (onnx.exists() and conf.exists()):
            detail = (r.stderr.strip() or r.stdout.strip())[-300:]
            raise RuntimeError(
                f"Could not download voice '{name}'. {detail}\n"
                f"Manual fix: in Anaconda Prompt run:  cd \"{self.dir}\"  then  "
                f"python -m piper.download_voices {name}")
        return onnx

    def _load(self, name):
        with self._voice_lock:
            if name not in self._voices:
                from piper import PiperVoice
                onnx = self.ensure_voice(name)
                self._voices[name] = PiperVoice.load(str(onnx))
            return self._voices[name]

    def _synth(self, lang, text):
        import numpy as np
        voice = self._load(self.voice_name(lang))
        kwargs = {}
        if abs(self.cfg.speed - 1.0) > 0.01:
            try:
                from piper import SynthesisConfig
            except ImportError:
                pass  # Older Piper versions only support the default speed.
            else:
                kwargs["syn_config"] = SynthesisConfig(length_scale=1.0 / max(self.cfg.speed, 0.3))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            voice.synthesize_wav(text, wf, **kwargs)
        buf.seek(0)
        with wave.open(buf, "rb") as wf:
            rate = wf.getframerate()
            data = wf.readframes(wf.getnframes())
        return np.frombuffer(data, dtype=np.int16), rate

    @staticmethod
    def _put(q, item, evt):
        while not evt.is_set():
            try:
                q.put(item, timeout=0.2)
                return
            except queue.Full:
                pass

    def _producer(self, evt, text_q, audio_q, responsive=False):
        chunker = SentenceChunker(responsive=responsive)
        prev = self.cfg.default_lang

        def speak_sentences(sentences):
            nonlocal prev
            for s in sentences:
                if evt.is_set():
                    return
                lang = detect_lang(s) if len(s.split()) >= 4 else None
                lang = lang or prev
                prev = lang
                self._put(audio_q, self._synth(lang, s), evt)

        try:
            while not evt.is_set():
                try:
                    item = text_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    speak_sentences(chunker.flush())
                    break
                speak_sentences(chunker.feed(item))
        except Exception as e:
            self.last_error = f"Voice error: {str(e)[:250]}"
            self.on_status(self.last_error)
        finally:
            self._put(audio_q, None, evt)

    def _consumer(self, evt, audio_q, on_done, sid):
        with self._state_lock:
            self._consumers += 1
        try:
            import sounddevice as sd
            while not evt.is_set():
                try:
                    item = audio_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break
                audio, rate = item
                if evt.is_set():
                    break
                sd.play(audio, rate)
                sd.wait()
        except Exception as e:
            self.last_error = f"Playback error: {str(e)[:250]}"
            self.on_status(self.last_error)
        finally:
            with self._state_lock:
                self._consumers -= 1
            if on_done:
                try:
                    on_done(sid)
                except Exception:
                    pass

    def start_stream(self, on_done=None, responsive=False):
        """Begin speaking text that arrives piece by piece. Returns a stream id."""
        self.stop()
        self.last_error = None
        evt = threading.Event()
        text_q = queue.Queue()
        audio_q = queue.Queue(maxsize=3)
        with self._state_lock:
            self._stream_id += 1
            sid = self._stream_id
            self._evt = evt
            self._text_q = text_q
        threading.Thread(target=self._producer, args=(evt, text_q, audio_q, responsive), daemon=True).start()
        threading.Thread(target=self._consumer, args=(evt, audio_q, on_done, sid), daemon=True).start()
        return sid

    def feed(self, text):
        q, evt = self._text_q, self._evt
        if q is not None and not evt.is_set() and text:
            q.put(text)

    def end_stream(self):
        q, evt = self._text_q, self._evt
        if q is not None and not evt.is_set():
            q.put(None)

    def speak(self, text, on_done=None):
        sid = self.start_stream(on_done)
        self.feed(text)
        self.end_stream()
        return sid

    def stop(self):
        self._evt.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass


# ----------------------------------------------------------------------
# PUSH-TO-TALK RECORDING
# ----------------------------------------------------------------------
class Recorder:
    RATE = 16000

    def __init__(self):
        self._frames = []
        self._stream = None

    @property
    def recording(self):
        return self._stream is not None

    def start(self):
        import sounddevice as sd
        self._frames = []

        def callback(indata, frames, time_info, status):
            self._frames.append(indata.copy())

        self._stream = sd.InputStream(samplerate=self.RATE, channels=1, dtype="int16", callback=callback)
        self._stream.start()

    def stop(self, path):
        """Stop and save a WAV file. Returns the path, or None if the clip was too short."""
        import numpy as np
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        if not self._frames:
            return None
        audio = np.concatenate(self._frames)
        self._frames = []
        if len(audio) < self.RATE * 0.4:
            return None
        _write_wav(path, audio, self.RATE)
        return path


def _write_wav(path, audio, rate):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(audio.tobytes())


# ----------------------------------------------------------------------
# AUTOMATIC END-OF-SPEECH RECORDING (voice activity detection)
# ----------------------------------------------------------------------
def record_utterance(path, cfg, should_stop, on_speech_start=None, boost_fn=None, idle_timeout=None):
    """Listen until you speak and then pause. Returns (path_or_None, reason).
    reason: "ok", "stopped", "timeout" or "too_short"."""
    import numpy as np
    import sounddevice as sd

    rate = 16000
    block = int(rate * 0.03)                    # 30 ms blocks
    pre = collections.deque(maxlen=10)          # 300 ms of audio before speech starts
    frames, noise_samples = [], []
    noise = None
    in_speech = False
    run = loud_blocks = silent = 0
    sil_limit = max(5, int(cfg.silence_ms / 30))
    max_blocks = int(cfg.max_utterance_s * 1000 / 30)
    started = time.time()

    with sd.InputStream(samplerate=rate, channels=1, dtype="int16", blocksize=block) as stream:
        while True:
            if should_stop():
                return None, "stopped"
            data, _ = stream.read(block)
            x = np.asarray(data, dtype=np.float32).reshape(-1)
            rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0

            if noise is None:                   # measure the room noise first (300 ms)
                noise_samples.append(rms)
                pre.append(np.array(data, copy=True))
                if len(noise_samples) >= 10:
                    noise = float(np.percentile(noise_samples, 30))
                continue

            boost = boost_fn() if boost_fn else 1.0
            if boost > 1.0:
                started = time.time()           # Cerry is talking: do not count this as idle time
            threshold = max(noise * 3.0, 180.0) * boost / max(cfg.vad_sensitivity, 0.2)
            loud = rms >= threshold

            if not in_speech:
                pre.append(np.array(data, copy=True))
                run = run + 1 if loud else 0
                if not loud:
                    noise = 0.98 * noise + 0.02 * rms
                if run >= 3:                    # about 90 ms of speech confirms it
                    in_speech = True
                    frames = list(pre)
                    loud_blocks, silent = run, 0
                    if on_speech_start:
                        on_speech_start()
                elif idle_timeout and time.time() - started > idle_timeout:
                    return None, "timeout"
            else:
                frames.append(np.array(data, copy=True))
                if loud:
                    silent = 0
                    loud_blocks += 1
                else:
                    silent += 1
                if silent >= sil_limit or len(frames) >= max_blocks:
                    break

    if loud_blocks < 8:                         # less than about 240 ms of speech
        return None, "too_short"
    if silent > 10:                             # keep only 300 ms of trailing silence
        frames = frames[:len(frames) - (silent - 10)]
    _write_wav(path, np.concatenate(frames).reshape(-1), rate)
    return path, "ok"


class Conversation:
    """Runs the listen -> transcribe loop in a background thread and reports events.

    Events sent to on_event(kind, *args):
      ("state", "listening" | "transcribing"), ("speech_start",), ("text", text),
      ("timeout",), ("error", message), ("ended",)
    Call turn_done() when Cerry has finished answering, so listening resumes.
    """

    def __init__(self, cfg, tmp_dir, transcribe_fn, on_event, is_speaking):
        self.cfg = cfg
        self.tmp = Path(tmp_dir)
        self.session_id = uuid.uuid4().hex
        self.transcribe_fn = transcribe_fn
        self.on_event = on_event
        self.is_speaking = is_speaking
        self.active = False
        self._ready = threading.Event()
        self._thread = None

    def start(self):
        self.active = True
        self._ready.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.active = False
        self._ready.set()

    def turn_done(self):
        self._ready.set()

    def _run(self):
        n = 0
        try:
            while self.active:
                if not self._ready.wait(0.2):
                    continue
                self.on_event("state", "listening")
                path = self.tmp / f"convo_{self.session_id}_{n % 2}.wav"
                n += 1
                path, reason = record_utterance(
                    path, self.cfg, lambda: not self.active,
                    on_speech_start=lambda: self.on_event("speech_start"),
                    boost_fn=lambda: 2.5 if self.is_speaking() else 1.0,
                    idle_timeout=self.cfg.idle_timeout_s or None)
                if reason == "stopped":
                    break
                if reason == "timeout":
                    self.on_event("timeout")
                    break
                if not path:
                    continue
                if not self.cfg.interrupt:
                    self._ready.clear()         # wait until Cerry has answered
                self.on_event("state", "transcribing")
                try:
                    text = (self.transcribe_fn(path) or "").strip()
                except Exception as e:
                    self.on_event("error", str(e)[:200])
                    self._ready.set()
                    continue
                finally:
                    try:
                        Path(path).unlink()
                    except OSError:
                        pass
                if len(text) < 2:
                    self._ready.set()
                    continue
                self.on_event("text", text)
        except Exception as e:
            self.on_event("error", f"Microphone problem: {str(e)[:200]}")
        finally:
            self.on_event("ended")


# ----------------------------------------------------------------------
# SPEECH TO TEXT (Groq Whisper or local faster-whisper)
# ----------------------------------------------------------------------
_LANG_NAMES = {"english": "en", "en": "en", "german": "de", "de": "de"}
_whisper_models = {}


def _norm_lang(value):
    return _LANG_NAMES.get((value or "").strip().lower())


def _transcribe_groq(path, cfg, get_client, language):
    client = get_client("groq").with_options(timeout=60, max_retries=0)

    def call(lang):
        with open(path, "rb") as f:
            kwargs = {"model": cfg.groq_model, "file": f, "response_format": "verbose_json"}
            if lang:
                kwargs["language"] = lang
            if cfg.stt_hint:
                kwargs["prompt"] = cfg.stt_hint
            return client.audio.transcriptions.create(**kwargs)

    result = call(language)
    if language is None:
        raw = getattr(result, "language", None)
        if raw and _norm_lang(raw) is None:      # detected something other than English/German
            result = call("en")
    return (getattr(result, "text", "") or "").strip()


def _get_whisper(size):
    if size not in _whisper_models:
        from faster_whisper import WhisperModel
        _whisper_models[size] = WhisperModel(size, device="cpu", compute_type="int8")
    return _whisper_models[size]


def _transcribe_local(path, cfg, language):
    model = _get_whisper(cfg.whisper_model)

    def run(lang):
        segments, info = model.transcribe(str(path), language=lang, vad_filter=True,
                                          beam_size=5, initial_prompt=cfg.stt_hint or None)
        return " ".join(s.text.strip() for s in segments).strip(), info.language

    text, detected = run(language)
    if language is None and detected not in ("en", "de"):
        text, _ = run("en")
    return text


def transcribe(path, cfg, get_client, local_only=False, on_status=None, isolated=False):
    engine = "local" if local_only else cfg.stt_engine
    language = None if cfg.stt_language == "auto" else cfg.stt_language
    if engine == "groq":
        try:
            return _transcribe_groq(path, cfg, get_client, language)
        except Exception as first_error:
            try:
                from importlib.util import find_spec
                if find_spec("faster_whisper") is None:
                    raise ImportError
            except ImportError:
                raise first_error
            if on_status:
                on_status(f"Groq failed ({str(first_error)[:80]}). Using local Whisper...")
    if on_status:
        on_status("Transcribing locally (the first run downloads the model)...")
    if isolated:
        import audio_engine
        result = audio_engine.analyze(path, cfg.whisper_model, language, on_status=on_status,
                                      hint=cfg.stt_hint)
        return " ".join(segment["text"] for segment in result["segs"])
    return _transcribe_local(path, cfg, language)


# ----------------------------------------------------------------------
# SETTINGS WINDOW
# ----------------------------------------------------------------------
def open_voice_dialog(parent, cfg, path, speaker, on_change=None):
    win = tk.Toplevel(parent)
    win.title("Voice settings")
    win.transient(parent)
    frm = ttk.Frame(win, padding=12)
    frm.pack(fill="both", expand=True)

    engine = tk.StringVar(value=cfg.stt_engine)
    lang = tk.StringVar(value=cfg.stt_language)
    groq_model = tk.StringVar(value=cfg.groq_model)
    wmodel = tk.StringVar(value=cfg.whisper_model)
    hint = tk.StringVar(value=cfg.stt_hint)
    en_voice = tk.StringVar(value=cfg.en_voice)
    de_voice = tk.StringVar(value=cfg.de_voice)
    speed = tk.DoubleVar(value=cfg.speed)
    auto_read = tk.BooleanVar(value=cfg.auto_read)
    silence = tk.DoubleVar(value=cfg.silence_ms)
    sens = tk.DoubleVar(value=cfg.vad_sensitivity)
    idle = tk.StringVar(value=str(cfg.idle_timeout_s))
    interrupt = tk.BooleanVar(value=cfg.interrupt)

    def row(r, label, widget):
        ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
        widget.grid(row=r, column=1, sticky="we", pady=3)

    ttk.Label(frm, text="Listening (speech to text)").grid(row=0, column=0, columnspan=2, sticky="w")
    row(1, "Engine", ttk.Combobox(frm, textvariable=engine, state="readonly", values=["groq", "local"], width=30))
    row(2, "Language", ttk.Combobox(frm, textvariable=lang, state="readonly", values=["auto", "en", "de"], width=30))
    row(3, "Groq model", ttk.Entry(frm, textvariable=groq_model, width=33))
    row(4, "Local Whisper size", ttk.Combobox(frm, textvariable=wmodel, state="readonly",
                                              values=["tiny", "base", "small", "medium"], width=30))
    row(5, "Vocabulary hint", ttk.Entry(frm, textvariable=hint, width=33))

    ttk.Label(frm, text="Speaking (Piper, offline)").grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))
    row(7, "English voice", ttk.Entry(frm, textvariable=en_voice, width=33))
    row(8, "German voice", ttk.Entry(frm, textvariable=de_voice, width=33))
    row(9, "Speed", ttk.Scale(frm, from_=0.7, to=1.4, variable=speed, orient="horizontal"))
    ttk.Checkbutton(frm, text="Read answers aloud automatically", variable=auto_read).grid(
        row=10, column=0, columnspan=2, sticky="w", pady=4)

    ttk.Label(frm, text="Conversation mode").grid(row=11, column=0, columnspan=2, sticky="w", pady=(12, 0))
    row(12, "Pause that ends your turn (ms)", ttk.Scale(frm, from_=500, to=2000, variable=silence, orient="horizontal"))
    row(13, "Microphone sensitivity", ttk.Scale(frm, from_=0.5, to=2.5, variable=sens, orient="horizontal"))
    row(14, "End after silence (seconds, 0 = never)", ttk.Entry(frm, textvariable=idle, width=10))
    ttk.Checkbutton(frm, text="Let me interrupt Cerry by speaking (use headphones)", variable=interrupt).grid(
        row=15, column=0, columnspan=2, sticky="w", pady=4)

    def apply():
        cfg.stt_engine, cfg.stt_language = engine.get(), lang.get()
        cfg.groq_model, cfg.whisper_model = groq_model.get().strip(), wmodel.get()
        cfg.stt_hint = hint.get().strip()
        cfg.en_voice, cfg.de_voice = en_voice.get().strip(), de_voice.get().strip()
        cfg.speed = round(float(speed.get()), 2)
        cfg.auto_read = auto_read.get()
        cfg.silence_ms = int(round(float(silence.get()) / 50.0) * 50)
        cfg.vad_sensitivity = round(float(sens.get()), 2)
        try:
            cfg.idle_timeout_s = max(0, int(idle.get()))
        except ValueError:
            cfg.idle_timeout_s = 90
        cfg.interrupt = interrupt.get()
        cfg.save(path)
        if on_change:
            on_change()

    def test(sample_lang):
        apply()
        sample = {"en": "Hello, I am Cerry. This is a test of my English voice.",
                  "de": "Hallo, ich bin Cerry. Das ist ein Test meiner deutschen Stimme."}[sample_lang]
        speaker.speak(sample)

    def download():
        apply()

        def job():
            try:
                for name in (cfg.en_voice, cfg.de_voice):
                    speaker.ensure_voice(name)
                speaker.on_status("Both voices are ready.")
            except Exception as e:
                speaker.on_status(f"Voice download failed: {str(e)[:250]}")
        threading.Thread(target=job, daemon=True).start()

    buttons = ttk.Frame(frm)
    buttons.grid(row=16, column=0, columnspan=2, sticky="we", pady=(10, 0))
    ttk.Button(buttons, text="Test English", command=lambda: test("en")).pack(side="left", padx=2)
    ttk.Button(buttons, text="Test German", command=lambda: test("de")).pack(side="left", padx=2)
    ttk.Button(buttons, text="Download voices", command=download).pack(side="left", padx=2)
    ttk.Button(buttons, text="Save", command=lambda: (apply(), win.destroy())).pack(side="right", padx=2)
    ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right", padx=2)
