"""Cerry chat: fast live answers + calm text speed (replaces cerry_typing).

Why: while an answer is being written, Cerry used to delete and redraw the WHOLE chat window about ten times per
second (and cerry_typing added more redraws). In a long chat that makes the window slow and the buttons sluggish.
Now only the answer that is being written is redrawn; everything above it stays untouched. Finished answers, links,
the text you selected and the scroll position are not touched at all while a reply is streaming.

Switch on:  "cerry_chat" in ai_workspace_data/plugins.json (remove "cerry_typing" - this plugin includes it)
Text speed: the "Text speed" box (Instant / Fast / Medium / Slow / Very slow), saved in ai_workspace_data/typing.json.
"""
import json
import queue
import time
import tkinter as tk
from tkinter import ttk

ORDER = 90          # outermost wrapper of render_chat, so it can skip the work of the wrappers below it
SPEEDS = {"Instant": 0, "Fast": 100, "Medium": 50, "Slow": 30, "Very slow": 18}   # characters per second
DEFAULT = "Fast"
TICK_MS = 80
CATCH_UP_SECONDS = 8        # if the model is far ahead, the display speeds up to catch up within about this time
MAX_HOLD_SECONDS = 25       # never delay the end of an answer longer than this
DRAG_TIMEOUT = 30           # same value as cerry_export: do not redraw under the mouse while text is being selected
MARK = "cc_live"


class HoldQueue(queue.Queue):
    """Same as Queue, but keeps back the final 'reply' event until the text on screen has caught up."""

    def __init__(self, should_hold):
        super().__init__()
        self.should_hold = should_hold

    def get_nowait(self):
        with self.mutex:
            first = self.queue[0] if self.queue else None
        if first is not None and first[0] == "reply" and self.should_hold():
            raise queue.Empty
        return super().get_nowait()


def install(ns):
    App = ns["App"]
    if App.__dict__.get("_cc_installed"):
        return
    App._cc_installed = True
    name = ns.get("APP_NAME", "Cherry")
    path = ns["DATA_DIR"] / "typing.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8")).get("speed", DEFAULT)
    except (OSError, ValueError):
        saved = DEFAULT
    if saved not in SPEEDS:
        saved = DEFAULT

    def cps(app):
        return SPEEDS.get(app._cc_choice.get(), 0)

    def sync(app):
        live = app.live
        if live is not app._cc_ref:
            app._cc_ref = live
            app._cc_shown = 0.0
            app._cc_hold_since = None

    def behind(app):
        live = app.live
        return live is not None and app._cc_shown < len(live["answer"]) - 1

    def should_hold(app):
        if not cps(app) or getattr(app, "convo_active", False) or not behind(app):
            app._cc_hold_since = None
            return False
        now = time.monotonic()
        if app._cc_hold_since is None:
            app._cc_hold_since = now
        return now - app._cc_hold_since < MAX_HOLD_SECONDS

    def changed(app):
        choice = app._cc_choice.get()
        try:
            path.write_text(json.dumps({"speed": choice}), encoding="utf-8")
        except OSError:
            pass
        rate = SPEEDS[choice]
        app.status.set("Text speed: " + (f"{choice} (about {rate} characters per second)" if rate else "instant"))

    def tick(app):
        sync(app)
        rate = cps(app)
        live = app.live
        if rate and live is not None:
            full = len(live["answer"])
            if app._cc_shown < full:
                speed = max(rate, (full - app._cc_shown) / CATCH_UP_SECONDS)
                before = int(app._cc_shown)
                app._cc_shown = min(float(full), app._cc_shown + speed * TICK_MS / 1000.0)
                if int(app._cc_shown) != before:
                    app.render_chat(keep_view="auto")
        app.after(TICK_MS, lambda: tick(app))

    def drop_mark(app):
        app._cc_state = None
        try:
            app.chat_w.mark_unset(MARK)
        except tk.TclError:
            pass

    def set_mark(app, live):
        """After a full redraw: remember where the live answer starts, so the next updates replace only that part."""
        w = app.chat_w
        try:
            idx = w.search(f"{name} [{live['label']}]: ", "end-1c", stopindex="1.0", backwards=True)
            if not idx:
                return drop_mark(app)
            w.mark_set(MARK, idx)
            w.mark_gravity(MARK, "left")
            app._cc_state = {"chat": app.chat, "n": len(app.chat["messages"]), "label": live["label"]}
        except tk.TclError:
            drop_mark(app)

    def fast_ok(app, live):
        st = app._cc_state
        if not st or st["chat"] is not app.chat or st["n"] != len(app.chat["messages"]) or st["label"] != live["label"]:
            return False
        try:
            return MARK in app.chat_w.mark_names()
        except tk.TclError:
            return False

    def dragging(app):
        t = getattr(app, "_ex_drag_at", None)
        return t is not None and time.time() - t < DRAG_TIMEOUT

    def fast_update(app, live, shown):
        w = app.chat_w
        try:
            pos = w.yview()
            stick = pos[1] >= 0.98
            w.config(state="normal")
            w.delete(MARK, "end")
            w.insert("end", f"{name} [{live['label']}]: \n", "message_meta")
            app.insert_markdown(shown + " \u258c\n", "bot")
            if live["reasoning"] and app.show_reason.get():
                w.insert("end", "\nReasoning:\n" + live["reasoning"] + "\n", "think")
            w.insert("end", "\n")
            w.config(state="disabled")
            if stick:
                w.see("end")
            else:
                w.yview_moveto(pos[0])
            return True
        except tk.TclError:
            try:
                w.config(state="disabled")
            except tk.TclError:
                pass
            return False

    inner = App.render_chat

    def render_chat(app, keep_view=False, *a, **k):
        live = app.live
        if live is None:
            if app._cc_state is not None:
                drop_mark(app)
            return inner(app, keep_view, *a, **k)
        rate = cps(app)
        if rate:
            sync(app)
        shown = live["answer"][:int(app._cc_shown)] if rate else live["answer"]
        if keep_view == "auto" and fast_ok(app, live):
            if dragging(app):
                app._ex_skipped = True
                return None
            if fast_update(app, live, shown):
                app._cc_fast += 1
                return None
        full = live["answer"]
        live["answer"] = shown
        try:
            result = inner(app, keep_view, *a, **k)
        finally:
            live["answer"] = full
        app._cc_full += 1
        set_mark(app, live)
        return result

    original_init = App.__init__

    def init(app, *a, **k):
        app._cc_ref, app._cc_shown, app._cc_hold_since = None, 0.0, None
        app._cc_state, app._cc_fast, app._cc_full = None, 0, 0
        original_init(app, *a, **k)
        app._cc_choice = tk.StringVar(value=saved)
        strip = ttk.Frame(app, padding=(6, 0))
        strip.pack(fill="x", before=app.inp.master)
        ttk.Label(strip, text="Text speed:").pack(side="left", padx=(2, 4))
        box = ttk.Combobox(strip, textvariable=app._cc_choice, state="readonly", width=10, values=list(SPEEDS))
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda e: changed(app))
        old = app.q                                  # keep the end of an answer back until it has been "typed"
        app.q = HoldQueue(lambda: should_hold(app))
        while not old.empty():
            app.q.put(old.get_nowait())
        app.after(TICK_MS, lambda: tick(app))

    App.__init__, App.render_chat = init, render_chat
