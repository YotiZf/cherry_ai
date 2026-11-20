"""Cerry dock: one tidy, collapsible panel instead of a dozen toolbar rows.

Every plugin adds its own row of buttons above the input box. After all plugins have started, this module moves those
rows into ONE panel with tabs (Chat, Memory, Media, Agents, Jobs, Models, Core), so the chat area gets its space back.
No plugin has to be changed: rows are recognised by the buttons they contain.

Always visible, whatever tab is open:
  * the header buttons   [Tools] [Agents] [Jobs] [Audio] [Memory] [Stop]
      Agents / Jobs / Audio / Memory open their windows; Stop stops every running agent task and job search.
      Clicking the tab "Agents" or "Jobs" opens its window as well.
  * one line with the two options you change most:
      Text speed: [Medium]     [x] Switch to the next model if there is no answer within [30] seconds

Settings: ai_workspace_data/dock.json
    {"mode": "tabs", "collapsed": false, "tab": "Chat", "open_on_tab": ["Agents", "Jobs"],
     "always_visible": ["text speed", "no answer within"]}
  mode "tabs" = the tabbed panel (default)     mode "flat" = the old layout, one row per plugin
  open_on_tab = tabs that open their window when you click them ([] = never)
  always_visible = words; a plugin row containing one of them stays on the always-visible line ([] = none)
Started by the plugin loader (cerri_extensions.py); it is not listed in plugins.json.
"""
import json
import tkinter as tk
from tkinter import ttk

TAB_ORDER = ["Chat", "Memory", "Media", "Agents", "Jobs", "Models", "Core", "Tools"]
RULES = [      # (tab, lower-case words that appear on the buttons/labels of a plugin row)
    ("Memory", ("auto memory", "memory...")),
    ("Agents", ("agent",)),
    ("Jobs", ("job search", "find jobs")),
    ("Media", ("paste image", "audio file", "audio model", "audio tasks", "/image", "speaker labels")),
    ("Models", ("next model", "no answer within")),
    ("Core", ("offline", "thinking:", "file & script", "activity")),
    ("Chat", ("copy answer", "export answer", "export chat", "text speed")),
]
QUICK = [("Agents", "agent..."), ("Jobs", "jobs..."), ("Audio", "audio tasks..."), ("Memory", "memory...")]
STOP_TEXTS = ("stop all agents", "stop job search")
INTERACTIVE = {"TButton", "TCheckbutton", "TCombobox", "TSpinbox", "TRadiobutton", "TEntry", "TScale", "TMenubutton"}
DEFAULT_ALWAYS = ["text speed", "no answer within"]


def walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from walk(child)


def load_state(path):
    state = {"mode": "tabs", "collapsed": False, "tab": "", "open_on_tab": ["Agents", "Jobs"],
             "always_visible": list(DEFAULT_ALWAYS)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in state:
                if k in data:
                    state[k] = data[k]
    except (OSError, ValueError):
        pass
    for key in ("open_on_tab", "always_visible"):
        if not isinstance(state[key], list):
            state[key] = []
    return state


def save_state(path, state):
    try:
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def wrap_base(original_init):
    """Install FIRST (before the plugins): remembers which widgets belong to the main window itself."""
    def init(app, *a, **k):
        original_init(app, *a, **k)
        try:
            app._dock_base = set(app.winfo_children())
        except Exception:
            app._dock_base = None
    return init


def text_of(widget):
    try:
        return str(widget.cget("text"))
    except Exception:
        return ""


def texts_of(widget):
    out = [text_of(w) for w in [widget] + list(walk(widget))]
    return " | ".join(t for t in out if t).lower()


def is_row(widget):
    try:
        if widget.winfo_class() != "TFrame":
            return False
        return any(w.winfo_class() in INTERACTIVE for w in walk(widget))
    except Exception:
        return False


def tab_of(widget):
    text = texts_of(widget)
    for tab, words in RULES:
        if any(w in text for w in words):
            return tab
    return "Tools"


def find_buttons(rows):
    """{lower-case button text: button} for all real buttons inside the plugin rows."""
    found = {}
    for row in rows:
        for w in walk(row):
            try:
                if w.winfo_class() == "TButton":
                    found.setdefault(text_of(w).strip().lower(), w)
            except Exception:
                continue
    return found


class Dock:
    def __init__(self, app, state_path, state):
        self.app, self.path, self.state = app, state_path, state
        self.tabs, self.quick, self.stops = {}, {}, []
        self.ready = False                     # tab clicks open windows only after start-up is finished
        try:                                   # the name of the selected tab must stay readable in every theme
            style = ttk.Style(app)
            style.map("TNotebook.Tab", foreground=[("selected", "#000000"), ("!selected", "#000000")])
        except tk.TclError:
            pass
        self.outer = ttk.Frame(app)
        try:
            self.outer.pack(fill="x", before=app.inp.master)
        except tk.TclError:
            self.outer.pack(fill="x")
        self.head = ttk.Frame(self.outer)
        self.head.pack(fill="x")
        self.toggle_btn = ttk.Button(self.head, width=9, command=self.toggle)
        self.toggle_btn.pack(side="left", padx=4, pady=2)
        self.options = ttk.Frame(self.outer)           # the always-visible options line (text speed, wait time)
        self.options.pack(fill="x", padx=4)
        self.nb = ttk.Notebook(self.outer)
        self.nb.bind("<<NotebookTabChanged>>", self.on_tab)
        self.refresh_button()
        if not self.state.get("collapsed"):
            self.nb.pack(fill="x", padx=4)

    def refresh_button(self):
        self.toggle_btn.config(text=("\u25b8 Tools" if self.state.get("collapsed") else "\u25be Tools"))

    def add_quick(self, buttons):
        """Always-visible launchers in the header."""
        for label, text in QUICK:
            real = buttons.get(text)
            if real is not None:
                self.quick[label] = real
                ttk.Button(self.head, text=label, width=8, command=lambda b=real: b.invoke()).pack(side="left", padx=2)
        self.stops = [b for t, b in buttons.items() if t in STOP_TEXTS]
        if self.stops:
            ttk.Button(self.head, text="\u25a0 Stop", width=7, command=self.stop_all).pack(side="left", padx=(10, 2))

    def stop_all(self):
        done = 0
        for b in self.stops:
            try:
                if str(b.cget("state")) != "disabled":
                    b.invoke()
                    done += 1
            except tk.TclError:
                continue
        try:
            self.app.status.set("Stop requested." if done else "Nothing is running.")
        except Exception:
            pass

    def group(self, title):
        if title not in self.tabs:
            frame = ttk.Frame(self.nb, padding=(4, 2))
            self.nb.add(frame, text=title)
            self.tabs[title] = frame
        return self.tabs[title]

    def toggle(self):
        self.state["collapsed"] = not self.state.get("collapsed")
        if self.state["collapsed"]:
            self.nb.pack_forget()
        else:
            self.nb.pack(fill="x", padx=4)
        self.refresh_button()
        save_state(self.path, self.state)

    def on_tab(self, _event=None):
        try:
            name = self.nb.tab(self.nb.select(), "text")
        except tk.TclError:
            return
        self.state["tab"] = name
        save_state(self.path, self.state)
        if self.ready and name in self.state.get("open_on_tab", []):
            button = self.quick.get(name)                    # the tab "Agents" opens the Agent window, ...
            if button is not None:
                try:
                    button.invoke()
                except tk.TclError:
                    pass

    def select_saved(self):
        want = self.state.get("tab")
        if want in self.tabs:
            try:
                self.nb.select(self.tabs[want])
            except tk.TclError:
                pass


def arrange(app, state_path):
    """Move the plugin rows into the dock. Returns the number of rows moved (0 = layout left unchanged)."""
    state = load_state(state_path)
    if state.get("mode") != "tabs":
        return 0
    base = getattr(app, "_dock_base", None)
    if not base:
        return 0
    rows = [w for w in app.winfo_children()
            if w not in base and not getattr(w, "_dock_skip", False) and is_row(w)]
    if not rows:
        return 0
    keep_visible = [w.lower() for w in state.get("always_visible", [])]
    grouped, visible = {}, []
    for row in rows:
        if keep_visible and any(w in texts_of(row) for w in keep_visible):
            visible.append(row)
        else:
            grouped.setdefault(tab_of(row), []).append(row)
    dock = Dock(app, state_path, state)
    # Tk rule: a widget shown inside a container that is not its parent must be HIGHER in the stacking order than
    # that container, otherwise the container covers it (empty tabs!).  So every moved row is raised.
    for row in visible:
        row.pack_forget()
        row.pack(in_=dock.options, side="left", padx=(0, 18), anchor="w")
        row.lift()
    for title in TAB_ORDER:
        for row in grouped.get(title, []):
            row.pack_forget()
            row.pack(in_=dock.group(title), side="left", padx=(0, 14), anchor="n")
            row.lift()
    dock.add_quick(find_buttons(rows))
    dock.select_saved()
    app._dock = dock
    app.after(1500, lambda: setattr(dock, "ready", True))
    return len(rows)
