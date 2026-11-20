"""Cerry audio engine - no GUI.  Decode, transcribe (faster-whisper) and label speakers (pyannote).

Used by cerry_audio.py (the window with the job list).  Everything that takes time reports its progress through
callbacks and can be stopped between steps:
    on_status(text)                       what is happening now ("Decoding audio ...", "Loading model ...")
    on_progress(done_seconds, total)      how far the transcription has come, in seconds of audio
    on_segment({start, end, text})        every transcribed sentence, as soon as it exists (live preview)
    on_step(name, completed, total)       progress of the speaker step
    should_stop() -> bool                 asked after every sentence; True raises Stopped
Needs: pip install faster-whisper        (speaker labels also: pip install pyannote.audio, plus HF_TOKEN in key.env)
"""
import bisect
import shutil
import time
from pathlib import Path

AUDIO_EXT = {'.mp3', '.wav', '.m4a', '.flac', '.ogg', '.oga', '.opus', '.aac', '.wma',
             '.mp4', '.mkv', '.webm', '.mov', '.amr'}
MODEL_CHOICES = ['tiny', 'base', 'small', 'medium', 'large-v3-turbo']
DIARIZATION_MODEL = 'pyannote/speaker-diarization-community-1'
DEVICE, COMPUTE = 'cpu', 'int8'          # use 'cuda' / 'float16' if you have an NVIDIA GPU
ATTACH_LIMIT = 20000                      # characters of transcript attached directly to the next message
MAX_GAP = 2.0                             # seconds of silence that start a new transcript line
_MODELS = {}
_PIPE = {}


class Stopped(Exception):
    """Raised when the user pressed Stop."""


def fmt_time(sec):
    sec = int(max(0, sec))
    return f'{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}'


def fmt_short(sec):
    """'45 s', '7 min 20 s', '1 h 05 min' - for 'about ... left'."""
    sec = int(max(0, sec))
    if sec < 60:
        return f'{sec} s'
    if sec < 3600:
        return f'{sec // 60} min {sec % 60:02d} s'
    return f'{sec // 3600} h {sec % 3600 // 60:02d} min'


def estimate(done, total, elapsed):
    """-> (fraction 0..1, speed in audio-seconds per second, seconds left or None)."""
    frac = min(max(done / total, 0.0), 1.0) if total else 0.0
    if elapsed <= 0 or done <= 0:
        return frac, 0.0, None
    speed = done / elapsed
    return frac, speed, max(total - done, 0.0) / speed


def get_model(name):
    if name not in _MODELS:
        from faster_whisper import WhisperModel
        _MODELS.clear()                   # keep only one model in RAM
        _MODELS[name] = WhisperModel(name, device=DEVICE, compute_type=COMPUTE)
    return _MODELS[name]


def model_is_loaded(name):
    return name in _MODELS


def _decode_with_av(path):
    """Fallback decoder that also works with old PyAV versions."""
    import av
    import numpy as np
    container = av.open(str(path))
    chunks = []

    def take(frames):
        if frames is None:
            return
        if not isinstance(frames, (list, tuple)):
            frames = [frames]
        for fr in frames:
            if fr is not None:
                chunks.append(fr.to_ndarray().reshape(-1))

    try:
        stream = next(s for s in container.streams if s.type == 'audio')
        resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
        for frame in container.decode(stream):
            take(resampler.resample(frame))
        try:
            take(resampler.resample(None))
        except Exception:
            pass
    finally:
        container.close()
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def decode(path):
    """Any audio/video file -> 16 kHz mono float32 array."""
    try:
        from faster_whisper import decode_audio
        return decode_audio(str(path), sampling_rate=16000)
    except TypeError as e:               # old PyAV: av.open() has no 'metadata_errors'
        if 'metadata_errors' not in str(e):
            raise
    return _decode_with_av(path)


def transcribe_file(path, model_name='small', language=None, on_progress=None, words=False,
                    on_segment=None, on_status=None, should_stop=None):
    """Decode once, transcribe, and keep the audio array for the speaker step."""
    say = on_status or (lambda t: None)
    stop = should_stop or (lambda: False)
    say('Decoding audio ...')
    audio = decode(path)
    if stop():
        raise Stopped()
    say('Loading the transcription model (the first time this downloads it) ...')
    model = get_model(model_name)
    if stop():
        raise Stopped()
    say('Transcribing ...')
    segments, info = model.transcribe(audio, language=language, beam_size=5, vad_filter=True,
                                      word_timestamps=words)
    duration = float(getattr(info, 'duration', 0) or 0) or len(audio) / 16000.0
    if on_progress:
        on_progress(0.0, duration)
    segs = []
    for seg in segments:
        if stop():
            raise Stopped()
        text = seg.text.strip()
        if text:
            w = [(x.start, x.end, x.word) for x in (getattr(seg, 'words', None) or [])]
            segs.append({'start': seg.start, 'end': seg.end, 'text': text, 'words': w})
            if on_segment:
                on_segment({'start': seg.start, 'end': seg.end, 'text': text})
        if on_progress:
            on_progress(min(seg.end, duration), duration)
    if on_progress:
        on_progress(duration, duration)
    return {'segs': segs, 'audio': audio, 'language': getattr(info, 'language', '?'),
            'probability': float(getattr(info, 'language_probability', 0) or 0),
            'duration': duration, 'model': model_name, 'speakers': 0}


def get_pipeline(token):
    if 'p' not in _PIPE:
        from pyannote.audio import Pipeline
        pipe = Pipeline.from_pretrained(DIARIZATION_MODEL, token=token)
        if DEVICE == 'cuda':
            import torch
            pipe.to(torch.device('cuda'))
        _PIPE['p'] = pipe
    return _PIPE['p']


def diarize(audio, num_speakers=None, token=None, on_step=None, should_stop=None):
    """Return a sorted list of (start, end, label) speech turns, one speaker at a time."""
    if not token:
        raise RuntimeError('HF_TOKEN is missing in key.env (needed once to download the speaker model).')
    import torch
    pipe = get_pipeline(token)
    wave = torch.from_numpy(audio).unsqueeze(0)
    kwargs = {'num_speakers': int(num_speakers)} if num_speakers else {}

    def hook(step_name, step_artifact=None, file=None, total=None, completed=None):
        if should_stop and should_stop():
            raise Stopped()
        if on_step:
            on_step(step_name, completed, total)

    data = {'waveform': wave, 'sample_rate': 16000}
    try:
        out = pipe(data, hook=hook, **kwargs)
    except TypeError:                    # an older pyannote without the hook argument
        out = pipe(data, **kwargs)
    ann = getattr(out, 'exclusive_speaker_diarization', None)
    if ann is None:
        ann = getattr(out, 'speaker_diarization', None)
    if ann is None:
        ann = out
    try:
        turns = [(t.start, t.end, s) for t, s in ann]
    except (TypeError, ValueError):
        turns = [(t.start, t.end, s) for t, _, s in ann.itertracks(yield_label=True)]
    return sorted(turns)


def speaker_at(t, turns, starts):
    """Speaker whose turn contains t; otherwise the nearest turn within 1 s; otherwise None."""
    i = bisect.bisect_right(starts, t) - 1
    if i >= 0 and turns[i][0] <= t <= turns[i][1]:
        return turns[i][2]
    best, dist = None, 1.0
    for j in (i, i + 1):
        if 0 <= j < len(turns):
            d = min(abs(t - turns[j][0]), abs(t - turns[j][1]))
            if d <= dist:
                best, dist = turns[j][2], d
    return best


def make_lines(res, turns=None):
    """Return (lines, number_of_speakers). Without turns: one line per segment, no speaker names."""
    if not turns:
        return [f"[{fmt_time(s['start'])}] {s['text']}" for s in res['segs']], 0
    starts = [t[0] for t in turns]
    names, units = {}, []
    for seg in res['segs']:
        parts = seg['words'] or [(seg['start'], seg['end'], ' ' + seg['text'])]
        for ws, we, w in parts:
            units.append((ws, we, w, speaker_at((ws + we) / 2, turns, starts)))
    lines, cur = [], None
    for ws, we, w, spk in units:
        if spk is None:
            spk = cur['spk'] if cur else turns[0][2]
        if cur and spk == cur['spk'] and ws - cur['end'] <= MAX_GAP:
            cur['text'] += w
            cur['end'] = we
        else:
            if cur:
                lines.append(cur)
            cur = {'start': ws, 'end': we, 'spk': spk, 'text': w}
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        text = ln['text'].strip()
        if not text:
            continue
        names.setdefault(ln['spk'], f'Speaker {len(names) + 1}')
        out.append(f"[{fmt_time(ln['start'])}] {names[ln['spk']]}: {text}")
    return out, len(names)


def analyze(path, model_name='small', language=None, label_speakers=False, num_speakers=None,
            token=None, on_progress=None, on_status=None, on_segment=None, on_step=None, should_stop=None):
    """Transcribe, optionally label speakers. Never fails just because the speaker step fails."""
    res = transcribe_file(path, model_name, language, on_progress, words=label_speakers,
                          on_segment=on_segment, on_status=on_status, should_stop=should_stop)
    turns, warning = None, ''
    if label_speakers and res['segs']:
        try:
            if on_status:
                on_status('Identifying speakers (this can take a while on a CPU) ...')
            turns = diarize(res['audio'], num_speakers, token, on_step, should_stop)
        except Stopped:
            raise
        except Exception as e:
            warning = f'Speaker labels skipped: {e}'
    res.pop('audio', None)
    res['lines'], res['speakers'] = make_lines(res, turns)
    res['warning'] = warning
    return res


def build_text(res, name):
    spk = f" | speakers: {res['speakers']}" if res.get('speakers') else ''
    head = (f"Audio transcript of {name} | language: {res['language']} "
            f"({res['probability']:.0%}) | duration: {fmt_time(res['duration'])}{spk} | "
            f"model: {res['model']}")
    return head + '\n' + '\n'.join(res['lines'])


def unique(folder, name):
    p = Path(folder) / name
    n = 2
    while p.exists():
        p = Path(folder) / f'{Path(name).stem}_{n}{Path(name).suffix}'
        n += 1
    return p


def save_transcript(res, src, docs_dir, audio_dir):
    """Copy the audio into the workspace and save the transcript where the workspace search finds it."""
    src = Path(src)
    Path(docs_dir).mkdir(parents=True, exist_ok=True)
    Path(audio_dir).mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, unique(audio_dir, src.name))
    text = build_text(res, src.name)
    out = unique(docs_dir, f'{src.stem}_transcript.txt')
    out.write_text(text, encoding='utf-8')
    return out, text
