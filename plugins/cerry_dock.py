"""Cerry dock: one tidy, collapsible panel instead of a dozen toolbar rows.

Every plugin adds its own row of buttons above the input box. After all plugins have started, this module moves those
rows into ONE panel with tabs (Chat, Memory, Media, Agents, Jobs, Models, Core), so the chat area gets its space back.
No plugin has to be changed: rows are recognised by the buttons they contain.

Always visible, whatever tab is open:
  * the header buttons   [Tools] [Agents] [Jobs] [Audio] [Memory] [Stop]
      Agents / Jobs / Audio / Memory open their windows; Stop stops every running agent task and job search.
      Clicking the tab "Agents" or "Jobs" opens its window as well.
The expanded panel distributes control groups across each tab. A horizontal scrollbar appears when needed.
Token limits, text speed and response deadlines share the Settings tab.

Settings: ai_workspace_data/dock.json
    {"mode": "tabs", "collapsed": false, "tab": "Chat", "open_on_tab": ["Agents", "Jobs"],
     "always_visible": ["text speed", "no answer within"]}
  mode "tabs" = the tabbed panel (default)     mode "flat" = the old layout, one row per plugin
  open_on_tab = tabs that open their window when you click them ([] = never)
  always_visible = legacy keywords selecting rows for the Settings tab
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
    state = {"mode": "tabs", "collapsed": True, "tab": "", "open_on_tab": ["Agents", "Jobs"],
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


class ToolPage(ttk.Frame):
    """A single strip that distributes groups across the available space."""
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, width=1, height=1, highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        self.track = ttk.Frame(self.canvas)
        self.item = self.canvas.create_window(0, 0, window=self.track, anchor="nw")
        self.scroll = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=self.scroll.set)
        self.track.bind("<Configure>", self.resize)
        self.canvas.bind("<Configure>", self.resize)

    def resize(self, _event):
        available = self.canvas.winfo_width()
        needed = self.track.winfo_reqwidth()
        width = max(available, needed)
        height = self.track.winfo_reqheight()
        self.canvas.itemconfigure(self.item, width=width)
        self.canvas.configure(height=height, scrollregion=(0, 0, width, height))
        if needed > available:
            self.scroll.pack(fill="x")
        else:
            self.scroll.pack_forget()
            self.canvas.xview_moveto(0)

    def add(self, row):
        column = len(self.track.grid_slaves())
        row.pack_forget()
        self.track.columnconfigure(column, weight=1)
        row.grid(in_=self.track, row=0, column=column, padx=4, sticky="w")
        row.lift()


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
        self.head.pack(side="bottom", fill="x")
        self.toggle_btn = ttk.Button(self.head, width=9, command=self.toggle)
        self.toggle_btn.pack(side="left", padx=4, pady=2)
        self.preview = False
        self.hover_job = None
        self.toggle_btn.bind("<Enter>", self.schedule_preview)
        app.bind("<Leave>", self.schedule_close, add="+")
        self.nb = ttk.Notebook(self.outer)
        self.nb.bind("<<NotebookTabChanged>>", self.on_tab)
        self.refresh_button()
        if not self.state.get("collapsed"):
            self.nb.pack(fill="x", padx=4)

    def refresh_button(self):
        self.toggle_btn.config(text=("Pin tools" if self.preview else
                                     "▴ Tools" if self.state.get("collapsed") else "▾ Tools"))

    def pointer_inside(self, widget):
        x, y = self.app.winfo_pointerxy()
        return (0 <= x - widget.winfo_rootx() < widget.winfo_width() and
                0 <= y - widget.winfo_rooty() < widget.winfo_height())

    def schedule_preview(self, _event):
        if self.state["collapsed"] and not self.preview and self.hover_job is None:
            self.hover_job = self.app.after(350, self.show_preview)

    def show_preview(self):
        self.hover_job = None
        if self.state["collapsed"] and self.pointer_inside(self.toggle_btn):
            self.preview = True
            self.nb.pack(fill="x", padx=4)
            self.refresh_button()

    def schedule_close(self, _event):
        if self.preview and self.hover_job is None:
            self.hover_job = self.app.after(250, self.close_preview)

    def close_preview(self):
        self.hover_job = None
        if self.preview and not self.pointer_inside(self.outer):
            self.preview = False
            self.nb.pack_forget()
            self.refresh_button()

    def add_quick(self, buttons):
        """Always-visible launchers in the header."""
        for label, text in QUICK:
            real = buttons.get(text)
            if real is not None:
                self.quick[label] = real
                ttk.Button(self.head, text=label, width=6, command=lambda b=real: b.invoke()).pack(side="left", expand=True, padx=2)
        self.stops = [b for t, b in buttons.items() if t in STOP_TEXTS]
        if self.stops:
            ttk.Button(self.head, text="\u25a0 Stop", width=6, command=self.stop_all).pack(side="right", padx=4, before=self.toggle_btn)

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
            frame = ToolPage(self.nb)
            self.nb.add(frame, text=title)
            self.tabs[title] = frame
        return self.tabs[title]

    def toggle(self):
        self.preview = False
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
    grouped = {}
    for row in rows:
        text = texts_of(row)
        title = "Settings" if "token control" in text or any(w in text for w in keep_visible) else tab_of(row)
        grouped.setdefault(title, []).append(row)
    dock = Dock(app, state_path, state)
    # Tk rule: a widget shown inside a container that is not its parent must be HIGHER in the stacking order than
    # that container, otherwise the container covers it (empty tabs!).  So every moved row is raised.
    for title in TAB_ORDER + ["Settings"]:
        for row in grouped.get(title, []):
            dock.group(title).add(row)
    dock.add_quick(find_buttons(rows))
    dock.select_saved()
    app._dock = dock
    app.after(1500, lambda: setattr(dock, "ready", True))
    return len(rows)
