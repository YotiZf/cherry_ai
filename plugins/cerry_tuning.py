"""Cerry tuning: thinking level, temperature and study depth - one row of controls above the chat.

  Thinking     [Default | Fast | Think | Deep]     how carefully the model works (see tuning_engine.py)
  Temperature  [Auto | 0.0 ... 2.0]                precise and repeatable (low) or more varied (high)
  Study depth  [Quick | Standard | Deep | Thorough]  how much of your documents is read to answer a question
  Writing style...                                 opens the window of the writing-training folder (cerry_style)
Thinking and temperature are used for normal chat answers (and for CV / cover-letter writing with its own temperature,
see "temp_writing" in tuning.json).  Settings are saved in ai_workspace_data/tuning.json.
Switch on: "cerry_tuning" in ai_workspace_data/plugins.json (needs tuning_engine.py next to it).
"""
import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import tuning_engine as te

ORDER = 45
TEMPS = ["Auto", "0.0", "0.2", "0.3", "0.5", "0.7", "0.9", "1.0", "1.2", "1.5"]


class _Completions:
    def __init__(self, real, owner):
        self.real, self.owner = real, owner

    def create(self, *args, **kwargs):
        return self.owner.tuner.call(self.real.create, self.owner.provider, args, kwargs)

    def __getattr__(self, name):
        return getattr(self.real, name)


class _Chat:
    def __init__(self, real, owner):
        self.completions = _Completions(real.completions, owner)
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)


class TuningClient:
    """Looks like the normal client; chat.completions.create adds thinking / temperature when they apply."""

    def __init__(self, real, provider, tuner):
        self._real, self.provider, self.tuner = real, provider, tuner
        self.chat = _Chat(real.chat, self)

    def with_options(self, *a, **k):
        return TuningClient(self._real.with_options(*a, **k), self.provider, self.tuner)

    def __getattr__(self, name):
        return getattr(self._real, name)


def install(ns):
    import doc_manager
    App = ns["App"]
    if App.__dict__.get("_tuning_plugin_installed"):
        return
    App._tuning_plugin_installed = True
    data = Path(ns["DATA_DIR"])
    cfg = dict(te.DEFAULTS)
    cfg.update(te.load_json(data / "tuning.json", {}))
    if cfg["thinking"] not in te.THINK_LEVELS:
        cfg["thinking"] = "Default"
    if cfg["depth"] not in te.DEPTH_LEVELS:
        cfg["depth"] = "Standard"
    base_prefix = str(ns.get("BASE_SYSTEM", "Your name is"))[:25]
    tuner = ns["_tuner"] = te.Tuner(cfg, data / "tuning_unsupported.json", base_prefix)

    def save():
        try:
            (data / "tuning.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ thinking + temperature: wrap the client
    original_get_client = ns["get_client"]
    ns["get_client"] = lambda provider: TuningClient(original_get_client(provider), provider, tuner)

    # ------------------------------------------------------------ study depth: how many passages are used
    Index = doc_manager.DocumentIndex
    original_search = Index.search

    def search(self, query, k=6, excluded=()):
        level = te.DEPTH.get(cfg["depth"], te.DEPTH["Standard"])
        default_call = (k == 6)                              # build_messages asks for 6; other callers keep their own k
        hits = original_search(self, query, level["k"] if default_call else k, excluded)
        if default_call and level["expand"] and hits:
            hits = te.expand_hits(self, hits)
        return hits
    Index.search = search

    # ------------------------------------------------------------ "Thorough": read whole files first, take notes
    original_start = App.start_generation

    def start_generation(app, text, base, target):
        try:
            wants = (cfg["depth"] == "Thorough" and app.turn.workspace_context and any(r.chunks for r in app.turn.workspace.index.records) and len(text) >= 15
                     and not getattr(app, "convo_active", False))
        except Exception:
            wants = False
        if not wants:
            return original_start(app, text, base, target)
        files = te.rank_files(app.turn.workspace.index, text[:1500], int(cfg["thorough_files"]),
                              app.turn.workspace.excluded)
        if not files:
            return original_start(app, text, base, target)
        streaming, routing = ns["streaming"], ns["routing"]
        token = streaming.CancelToken()
        app.token, app.busy = token, True
        app.send_btn.config(state="disabled")
        app.stop_btn.config(state="normal")
        model, policy = app.turn.model, app.turn.policy
        box = {"done": False, "notes": "", "error": "", "stopped": False}

        def ask(system, user):
            order, _ = routing.build_order(ns["MODELS"], model, policy)
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            return streaming.run_chat(order, msgs, ns["get_client"], token, policy, None, None)["answer"]

        def job():
            try:
                box["notes"] = te.study(text[:1500], files, app.turn.workspace.index.full_text, ask, int(cfg["section_chars"]),
                                        int(cfg["thorough_sections"]), lambda t: app.q.put(("status", t)), token.is_set)
            except (InterruptedError, streaming.Cancelled):
                box["stopped"] = True
            except Exception as e:
                box["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            box["done"] = True
        threading.Thread(target=job, daemon=True).start()

        def poll():
            if not box["done"]:
                app.after(300, poll)
                return
            app.busy = False
            if box["stopped"]:
                app.send_btn.config(state="normal")
                app.stop_btn.config(state="disabled")
                app.status.set("Stopped while studying the documents.")
                app.render_chat()
                return
            if box["error"]:
                app.show("Deep study failed (" + box["error"] + "). Answering with the normal search instead.")
            if box["notes"]:
                app.pending_files.append((f"Study notes ({len(files)} files read completely)", box["notes"][:30000]))
            original_start(app, text, base, target)
        app.status.set(f"Studying {len(files)} document(s) completely ...")
        app.after(300, poll)

    App.start_generation = start_generation

    # ------------------------------------------------------------ the row of controls
    original_init = App.__init__

    def init(app, *a, **k):
        original_init(app, *a, **k)
        vars_ = {"thinking": tk.StringVar(value=cfg["thinking"]), "temperature": tk.StringVar(value=str(cfg["temperature"])),
                 "depth": tk.StringVar(value=cfg["depth"])}

        def changed(key, hint):
            def handler(_e=None):
                value = vars_[key].get().strip()
                if key == "temperature":
                    value = "Auto" if te.parse_temperature(value) is None else str(te.parse_temperature(value))
                    vars_[key].set(value)
                cfg[key] = value
                save()
                app.status.set(hint(value))
            return handler
        strip = app.sidebar.tool_row("Response settings")
        ttk.Label(strip, text="Thinking:").pack(side="left")
        box = ttk.Combobox(strip, textvariable=vars_["thinking"], values=te.THINK_LEVELS, state="readonly", width=9)
        box.pack(side="left", padx=(3, 12))
        box.bind("<<ComboboxSelected>>", changed("thinking", lambda v: f"Thinking: {v}" + (
            " - the instruction is added to every chat request" + (", and sent as reasoning_effort to " + ", ".join(cfg["effort_providers"])
                                                                   if cfg["send_effort"] and v != "Default" else "") if v != "Default" else "")))
        strip = app.sidebar.tool_row("Response settings")
        ttk.Label(strip, text="Temperature:").pack(side="left")
        temp = ttk.Combobox(strip, textvariable=vars_["temperature"], values=TEMPS, width=6)
        temp.pack(side="left", padx=(3, 12))
        for seq in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
            temp.bind(seq, changed("temperature", lambda v: "Temperature: " + ("the model's default" if v == "Auto" else v)))
        strip = app.sidebar.tool_row("Response settings")
        ttk.Label(strip, text="Study depth:").pack(side="left")
        depth = ttk.Combobox(strip, textvariable=vars_["depth"], values=te.DEPTH_LEVELS, state="readonly", width=10)
        depth.pack(side="left", padx=(3, 12))
        depth.bind("<<ComboboxSelected>>", changed("depth", lambda v: {
            "Quick": "Study depth Quick: the 3 best passages.", "Standard": "Study depth Standard: the 6 best passages.",
            "Deep": "Study depth Deep: 12 passages, each with the text around it.",
            "Thorough": "Study depth Thorough: the most relevant files are read completely first (slower, many model calls)."}[v]))

        app._tuning_vars = vars_

    App.__init__ = init
