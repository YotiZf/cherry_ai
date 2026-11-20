# -*- coding: utf-8 -*-
"""
Created on Thu Oct 29 14:49:41 2026

@author: panaj
"""

import re
import threading

from routing import classify_error, should_fallback


class Cancelled(Exception):
    def __init__(self, answer="", reasoning="", label=""):
        super().__init__("Cancelled")
        self.answer = answer
        self.reasoning = reasoning
        self.label = label


class AllFailed(RuntimeError):
    pass


class CancelToken:
    """Set by the Stop button. Also closes any open network stream (best effort)."""

    def __init__(self):
        self._ev = threading.Event()
        self._closers = []
        self._lock = threading.Lock()

    def is_set(self):
        return self._ev.is_set()

    def register(self, fn):
        with self._lock:
            self._closers.append(fn)
        if self._ev.is_set():
            try:
                fn()
            except Exception:
                pass

    def unregister(self, fn):
        with self._lock:
            if fn in self._closers:
                self._closers.remove(fn)

    def cancel(self):
        self._ev.set()
        with self._lock:
            closers = list(self._closers)
        for fn in closers:
            try:
                fn()
            except Exception:
                pass


class ThinkSplitter:
    """Separates <think>...</think> text from the visible answer while streaming."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.in_think = False
        self.saw_open = False

    def _put(self, text, vis, think):
        if self.in_think:
            return vis, think + text
        return vis + text, think

    def feed(self, s):
        self.buf += s
        vis, think = "", ""
        while True:
            tag = self.CLOSE if self.in_think else self.OPEN
            idx = self.buf.find(tag)
            if idx >= 0:
                vis, think = self._put(self.buf[:idx], vis, think)
                self.buf = self.buf[idx + len(tag):]
                if not self.in_think:
                    self.saw_open = True
                self.in_think = not self.in_think
                continue
            keep = 0
            for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-k:]):
                    keep = k
                    break
            cut = len(self.buf) - keep
            vis, think = self._put(self.buf[:cut], vis, think)
            self.buf = self.buf[cut:]
            return vis, think

    def flush(self):
        rest, self.buf = self.buf, ""
        if self.in_think:
            return "", rest
        return rest, ""


def _reasoning_of(obj):
    r = getattr(obj, "reasoning_content", None) or getattr(obj, "reasoning", None)
    return r if isinstance(r, str) else ""


def _split_closing_only(answer, saw_open):
    """Some local R1 builds output reasoning followed by </think> with no opening tag."""
    if not saw_open and "</think>" in answer:
        head, _, tail = answer.partition("</think>")
        return tail.strip(), head.strip()
    return answer.strip(), ""


def _complete_once(client, model_id, messages, on_delta):
    r = client.chat.completions.create(model=model_id, messages=messages)
    msg = r.choices[0].message
    out = msg.content or ""
    reasoning = _reasoning_of(msg).strip()
    sp = ThinkSplitter()
    vis, th = sp.feed(out)
    v2, t2 = sp.flush()
    vis, th = vis + v2, th + t2
    vis, extra = _split_closing_only(vis, sp.saw_open)
    reasoning = "\n".join(x for x in (reasoning, th.strip(), extra) if x)
    if on_delta:
        if reasoning:
            on_delta("reasoning", reasoning)
        if vis:
            on_delta("answer", vis)
    return vis, reasoning


def stream_completion(client, model_id, messages, token, on_delta=None):
    """Return (answer, reasoning). Raises Cancelled (with partial text) if stopped."""
    def emit(kind, text):
        if on_delta and text:
            on_delta(kind, text)

    try:
        stream = client.chat.completions.create(model=model_id, messages=messages, stream=True)
    except Exception as e:
        if "stream" in str(e).lower() and not token.is_set():
            return _complete_once(client, model_id, messages, on_delta)
        raise

    answer, reasoning = [], []
    splitter = ThinkSplitter()
    token.register(stream.close)
    try:
        for chunk in stream:
            if token.is_set():
                break
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            r = _reasoning_of(delta)
            if r:
                reasoning.append(r)
                emit("reasoning", r)
            c = getattr(delta, "content", None) or ""
            if c:
                vis, th = splitter.feed(c)
                if th:
                    reasoning.append(th)
                    emit("reasoning", th)
                if vis:
                    answer.append(vis)
                    emit("answer", vis)
    except Exception:
        if not token.is_set():
            raise
    finally:
        token.unregister(stream.close)
        try:
            stream.close()
        except Exception:
            pass

    vis, th = splitter.flush()
    if vis:
        answer.append(vis)
        emit("answer", vis)
    if th:
        reasoning.append(th)
        emit("reasoning", th)

    if token.is_set():
        raise Cancelled("".join(answer).strip(), "".join(reasoning).strip())

    ans, extra = _split_closing_only("".join(answer), splitter.saw_open)
    reas = "\n".join(x for x in ("".join(reasoning).strip(), extra) if x)
    return ans, reas


def run_chat(order, messages, get_client, token, policy, on_status=None, on_delta=None):
    """Try models in `order`. Returns dict(answer, reasoning, label)."""
    errors = []
    for m in order:
        if token.is_set():
            raise Cancelled()
        if on_status:
            on_status(f"Trying {m['label']}...")
        if on_delta:
            on_delta("reset", m["label"])
        try:
            client = get_client(m["provider"])
            ans, reas = stream_completion(client, m["id"], messages, token, on_delta)
            if not ans.strip() and not reas.strip():
                raise RuntimeError("the model returned an empty answer")      # try the next model
            return {"answer": ans, "reasoning": reas, "label": m["label"]}
        except Cancelled as c:
            c.label = m["label"]
            raise
        except Exception as e:
            kind = classify_error(e)
            errors.append(f"{m['label']} [{kind}]: {str(e)[:160]}")
            if on_status:
                on_status(f"Failed - {errors[-1]}")
            if not should_fallback(kind, policy):
                errors.append(f"Fallback is not allowed after '{kind}' errors (see Routing settings).")
                break
    raise AllFailed("All models failed:\n" + "\n".join(errors))