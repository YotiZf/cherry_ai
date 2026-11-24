"""Cerry context menu + resizable input area.

1. Right-click in any text box (chat, input, editors, settings windows) shows
   Cut / Copy / Paste / Delete / Select all.
2. Resize the box where you type with the mouse: move the pointer to the TOP EDGE of the input box
   (it turns into an up/down arrow) or onto the resize label in the Display sidebar section, hold the left button and drag
   up (taller) or down (shorter). Double-click resets it. The size is remembered.

The Display sidebar section also has taller, shorter and reset buttons.

Switch it on by adding  "cerry_contextmenu"  to the list in  ai_workspace_data/plugins.json
"""
import json
import sys
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

EDITABLE_CLASSES = ("Text", "Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox")
DEFAULT_LINES = 4
MIN_LINES = 2
EDGE_PX = 8                 # the top edge of the input box works as a handle
MIN_CHAT_PX = 70            # the chat is never squeezed smaller than this
MAX_LINES = 40
STEP_LINES = 2             # the [taller] / [shorter] buttons
INSTALL_FLAG = "_ctxmenu_installed"     # NOT "_cm_installed": cerry_models used that name too and blocked this plugin


# ----------------------------------------------------------------------
# context menu
# ----------------------------------------------------------------------
def widget_state(w):
    """Return (can_edit, has_selection) for a Text / Entry style widget."""
    try:
        can_edit = str(w.cget("state")) == "normal"
    except tk.TclError:
        can_edit = True
    try:
        if w.winfo_class() == "Text":
            has_sel = bool(w.tag_ranges("sel"))
        else:
            has_sel = bool(w.selection_present())
    except tk.TclError:
        has_sel = False
    return can_edit, has_sel


def select_all(w):
    w.focus_set()
    if w.winfo_class() == "Text":
        w.tag_add("sel", "1.0", "end-1c")
        w.mark_set("insert", "end-1c")
    else:
        w.select_range(0, "end")
        w.icursor("end")


def delete_selection(w):
    try:
        w.delete("sel.first", "sel.last")
    except tk.TclError:
        pass


def paste(w, is_input):
    w.focus_set()
    try:
        # the same key press as Ctrl+V, so image pasting (cerry_vision) keeps working in the input box
        w.event_generate("<Control-Key-v>" if is_input else "<<Paste>>")
    except tk.TclError:
        w.event_generate("<<Paste>>")


def build_menu(w, app, colors):
    can_edit, has_sel = widget_state(w)
    is_input = w is getattr(app, "inp", None)
    menu = tk.Menu(w, tearoff=0, **colors)

    def state(enabled):
        return "normal" if enabled else "disabled"

    menu.add_command(label="Cut", accelerator="Ctrl+X", state=state(can_edit and has_sel),
                     command=lambda: w.event_generate("<<Cut>>"))
    menu.add_command(label="Copy", accelerator="Ctrl+C", state=state(has_sel),
                     command=lambda: w.event_generate("<<Copy>>"))
    menu.add_command(label="Paste", accelerator="Ctrl+V", state=state(can_edit),
                     command=lambda: paste(w, is_input))
    menu.add_command(label="Delete", state=state(can_edit and has_sel), command=lambda: delete_selection(w))
    menu.add_separator()
    menu.add_command(label="Select all", accelerator="Ctrl+A", command=lambda: select_all(w))
    return menu


def theme_colors(app):
    try:
        style = ttk.Style(app)
        bg = style.lookup("TFrame", "background") or None
        fg = style.lookup("TLabel", "foreground") or None
        sel = style.lookup("Treeview", "background", ["selected"]) or None
        if not (bg and fg):
            return {}
        if sel:
            return {"bg": bg, "fg": fg, "activebackground": sel, "activeforeground": fg}
        return {"bg": bg, "fg": fg, "activebackground": fg, "activeforeground": bg}
    except tk.TclError:
        return {}


def install_menu(app):
    def popup(event):
        w = event.widget
        try:
            if w.winfo_class() not in EDITABLE_CLASSES:
                return None
            w.focus_set()
            menu = build_menu(w, app, theme_colors(app))
        except tk.TclError:
            return None
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    sequences = ["<Button-3>", "<Shift-F10>"]
    if sys.platform == "darwin":
        sequences += ["<Button-2>", "<Control-Button-1>"]
    for cls in EDITABLE_CLASSES:
        for seq in sequences:
            app.bind_class(cls, seq, popup, add="+")
    for widget in (getattr(app, "chat_w", None), getattr(app, "inp", None)):      # the two main boxes, directly
        if widget is not None:
            for seq in sequences:
                widget.bind(seq, popup, add="+")


# ----------------------------------------------------------------------
# resizing the input area with the mouse (drag, buttons, or Ctrl+Up / Ctrl+Down)
# ----------------------------------------------------------------------
def line_height(widget):
    try:
        return max(10, tkfont.Font(font=widget.cget("font")).metrics("linespace"))
    except tk.TclError:
        return 18


def install_resize(app, path, saved_lines):
    inp, chat = app.inp, app.chat_w
    # Without this the chat insists on its default size and the input box gets squeezed out instead.
    chat.configure(height=6)
    inp.configure(height=saved_lines)
    normal_cursor = str(inp.cget("cursor")) or "xterm"
    drag = {}

    def set_lines(n):
        """Set the height of the input box; refuse (and undo) when the chat would become too small."""
        n = max(MIN_LINES, min(MAX_LINES, int(n)))
        old = int(inp.cget("height"))
        if n == old:
            return old
        inp.configure(height=n)
        if n > old:
            app.update_idletasks()
            if chat.winfo_height() < MIN_CHAT_PX:
                inp.configure(height=old)
                app.update_idletasks()
                return old
        return n

    def grow_to(target):
        current = int(inp.cget("height"))
        if target <= current:
            set_lines(target)
            return
        while current < target:                         # one line at a time, so we stop exactly where the room ends
            got = set_lines(current + 1)
            if got == current:
                break
            current = got

    def begin(y_root):
        drag["y"] = y_root
        drag["lines"] = int(inp.cget("height"))

    def move(y_root):
        per_line = line_height(inp)
        grow_to(drag["lines"] + round((drag["y"] - y_root) / per_line))        # drag up = taller

    def finish():
        drag.clear()
        try:
            path.write_text(json.dumps({"input_lines": int(inp.cget("height"))}), encoding="utf-8")
        except OSError:
            pass

    def step(delta):
        grow_to(int(inp.cget("height")) + delta)
        finish()

    def reset():
        set_lines(DEFAULT_LINES)
        finish()

    bar = app.sidebar.tool_row("Display")
    grip = ttk.Label(bar, text="Drag to resize the message box", anchor="center",
                     cursor="sb_v_double_arrow", style="Muted.TLabel")
    grip.pack(side="left", fill="x", expand=True)
    for text, fn in (("\u25b2 taller", lambda: step(STEP_LINES)), ("\u25bc shorter", lambda: step(-STEP_LINES)),
                     ("reset", reset)):
        ttk.Button(bar, text=text, width=9, command=fn).pack(side="right", padx=2)
    grip.bind("<ButtonPress-1>", lambda e: begin(e.y_root))
    grip.bind("<B1-Motion>", lambda e: move(e.y_root) if drag else None)
    grip.bind("<ButtonRelease-1>", lambda e: finish())
    grip.bind("<Double-Button-1>", lambda e: reset())

    # 2) the top edge of the input box itself (the mouse pointer changes to an up/down arrow)
    def on_motion(e):
        if drag:
            return
        want = "sb_v_double_arrow" if e.y <= EDGE_PX else normal_cursor
        if str(inp.cget("cursor")) != want:
            inp.configure(cursor=want)

    def on_leave(e):
        if not drag:
            inp.configure(cursor=normal_cursor)

    def on_press(e):
        if e.y <= EDGE_PX:
            begin(e.y_root)
            return "break"
        return None

    def on_drag(e):
        if drag:
            move(e.y_root)
            return "break"
        return None

    def on_release(e):
        if drag:
            finish()
            inp.configure(cursor=normal_cursor)
            return "break"
        return None

    def on_double(e):
        if e.y <= EDGE_PX:
            reset()
            return "break"
        return None

    for seq, fn in (("<Motion>", on_motion), ("<Leave>", on_leave), ("<ButtonPress-1>", on_press),
                    ("<B1-Motion>", on_drag), ("<ButtonRelease-1>", on_release), ("<Double-Button-1>", on_double)):
        inp.bind(seq, fn, add="+")

    # Ctrl+Up / Ctrl+Down in the input box: taller / shorter
    inp.bind("<Control-Up>", lambda e: (step(STEP_LINES), "break")[1], add="+")
    inp.bind("<Control-Down>", lambda e: (step(-STEP_LINES), "break")[1], add="+")

    return bar


# ----------------------------------------------------------------------
def install(ns):
    App = ns["App"]
    if App.__dict__.get(INSTALL_FLAG):
        return
    setattr(App, INSTALL_FLAG, True)
    path = ns["DATA_DIR"] / "contextmenu.json"
    try:
        saved = int(json.loads(path.read_text(encoding="utf-8")).get("input_lines", DEFAULT_LINES))
    except (OSError, ValueError, TypeError):
        saved = DEFAULT_LINES
    saved = max(MIN_LINES, min(40, saved))
    orig_init = App.__init__

    def init(app, *a, **k):
        orig_init(app, *a, **k)
        install_menu(app)
        install_resize(app, path, saved)

    App.__init__ = init
