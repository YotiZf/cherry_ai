"""Scrollable workspace controls with a draggable left-hand divider."""
import tkinter as tk
from tkinter import ttk

SECTION_ORDER = ("Token control", "Response settings", "Memory", "Workspace settings", "Files and images",
                 "Audio and voice", "Chat and export", "Agents", "Jobs", "Display")
DEFAULT_COLLAPSED = SECTION_ORDER[1:]


class WorkspaceSidebar:
    def __init__(self, app, settings, save):
        self.app, self.settings, self.save = app, settings, save
        width = settings.get("sidebar_width", 250)
        self.width = max(200, min(460, width)) if isinstance(width, int) else 250
        self.panel = ttk.Frame(app, width=self.width)
        self.panel.pack(side="left", fill="y")
        self.panel.pack_propagate(False)
        self.rail = ttk.Frame(app, cursor="sb_h_double_arrow", relief="ridge", borderwidth=1)
        self.rail.pack(side="left", fill="y")
        self.toggle = ttk.Button(self.rail, text="‹", width=2, command=self.toggle_panel)
        self.toggle.pack(anchor="n")
        self.preview = False
        self.hover_job = None
        for widget in (self.rail, self.toggle):
            widget.bind("<Enter>", self.schedule_preview)
        app.bind("<Leave>", self.schedule_close, add="+")
        self.rail.bind("<ButtonPress-1>", self.start_drag)
        self.rail.bind("<B1-Motion>", self.drag)
        self.rail.bind("<ButtonRelease-1>", self.finish_drag)
        bar = ttk.Scrollbar(self.panel)
        bar.pack(side="right", fill="y")
        self.canvas = tk.Canvas(self.panel, highlightthickness=0, yscrollincrement=24, yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.configure(command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas)
        self.navigation = ttk.Frame(self.content)
        self.navigation.pack(fill="x")
        self.tools = ttk.Frame(self.content, padding=(8, 2))
        self.tools.pack(fill="x")
        self.sections = {}
        self.rows = []
        self.wheel_tag = "CherrySidebarWheel"
        app.bind_class(self.wheel_tag, "<MouseWheel>", self.scroll)
        item = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(item, width=e.width))
        self.content.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.collapsed = False
        if settings.get("sidebar_collapsed"):
            self.set_collapsed(True)

    def scroll(self, event):
        if event.delta:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def tool_row(self, title):
        """Create each plugin's original controls inside the scrolling canvas."""
        if title not in self.sections:
            section = ttk.Frame(self.tools)
            section.pack(fill="x", pady=2)
            body = ttk.Frame(section)
            section.columnconfigure(0, weight=1)
            body.grid(row=1, column=0, sticky="ew")
            if title != "Token control":
                button = ttk.Button(section, style="SidebarSection.TButton")
                button.grid(row=0, column=0, sticky="ew")
                collapsed = self.settings.get("collapsed_tool_sections", DEFAULT_COLLAPSED)
                self.disclosure(button, body, title, expanded=title not in collapsed,
                                on_change=lambda opened: self.save_section(title, opened))
            self.sections[title] = section, body
        row = ttk.Frame(self.sections[title][1], padding=(4, 2))
        row.pack(fill="x")
        self.rows.append(row)
        return row

    def save_section(self, title, opened):
        collapsed = set(self.settings.get("collapsed_tool_sections", DEFAULT_COLLAPSED))
        if opened:
            collapsed.discard(title)
        else:
            collapsed.add(title)
        self.settings["collapsed_tool_sections"] = sorted(collapsed)
        self.save(self.settings)

    def finish_tools(self):
        """Lay out the installed controls once, after the plugins have initialized."""
        self.arrange(self.navigation_grid)
        stops = [button for row in self.rows for button in row.pack_slaves()
                 if isinstance(button, ttk.Button) and button.cget("text") in ("Stop all agents", "Stop job search")]
        if len(stops) > 1:
            def stop_tasks():
                for button in stops:
                    if button.instate(["!disabled"]):
                        button.invoke()
            ttk.Button(self.tool_row("Agents"), text="Stop agents and job searches",
                       command=stop_tasks).pack(fill="x")
        for title in SECTION_ORDER:
            if title in self.sections:
                self.sections[title][0].pack_forget()
        for title in SECTION_ORDER:
            if title in self.sections:
                self.sections[title][0].pack(fill="x", pady=2)
        for row in self.rows:
            for widget in row.pack_slaves():
                widget.pack_configure(side="top", fill="x", expand=False, padx=2, pady=1)
                if isinstance(widget, ttk.Label):
                    widget.configure(wraplength=167)
                elif isinstance(widget, ttk.Button):
                    widget.configure(style="Sidebar.TButton")

        # Run before the combobox class binding, which otherwise changes a
        # setting when the user only intended to scroll past it.
        stack = [self.canvas]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if isinstance(widget, tk.Listbox):
                widget.bind("<MouseWheel>", self.scroll_history, add="+")
                continue
            tags = widget.bindtags()
            widget.bindtags((tags[0], self.wheel_tag, *tags[1:]))

    def scroll_history(self, event):
        first, last = event.widget.yview()
        if (event.delta > 0 and first <= 0) or (event.delta < 0 and last >= 1):
            return self.scroll(event)

    def set_collapsed(self, collapsed):
        if self.collapsed == collapsed:
            return
        self.collapsed = collapsed
        if collapsed:
            self.panel.pack_forget()
        else:
            self.panel.pack(side="left", fill="y", before=self.rail)
        self.toggle.configure(text="›" if collapsed else "‹")

    def persist(self):
        self.settings.update(sidebar_width=self.width, sidebar_collapsed=self.collapsed)
        self.save(self.settings)

    def toggle_panel(self):
        if self.preview:
            self.preview = False
            self.toggle.configure(text="‹")
        else:
            self.set_collapsed(not self.collapsed)
        self.persist()

    def schedule_preview(self, _event):
        if self.collapsed and self.hover_job is None:
            self.hover_job = self.app.after(350, self.show_preview)

    def pointer_inside(self, widget):
        x, y = self.app.winfo_pointerxy()
        return (0 <= x - widget.winfo_rootx() < widget.winfo_width()
                and 0 <= y - widget.winfo_rooty() < widget.winfo_height())

    def show_preview(self):
        self.hover_job = None
        if self.collapsed and self.pointer_inside(self.rail):
            self.set_collapsed(False)
            self.preview = True
            self.toggle.configure(text="+")

    def schedule_close(self, _event):
        if self.preview and self.hover_job is None:
            self.hover_job = self.app.after(220, self.close_preview)

    def close_preview(self):
        self.hover_job = None
        if self.preview and not (self.pointer_inside(self.panel) or self.pointer_inside(self.rail)):
            self.preview = False
            self.set_collapsed(True)

    def start_drag(self, event):
        self.preview = False
        if not self.collapsed:
            self.toggle.configure(text="‹")
        self.offset = event.x_root - (0 if self.collapsed else self.panel.winfo_width())

    def drag(self, event):
        width = event.x_root - self.offset
        self.set_collapsed(width < 90)
        if not self.collapsed:
            self.width = max(200, min(width, 460, self.app.winfo_width() // 3))
            self.panel.configure(width=self.width)

    def finish_drag(self, _event):
        self.persist()

    def arrange(self, top):
        # Plugins append to the original model/workspace grid during startup.
        # Keep their controls and callbacks, then stack each section for the sidebar.
        row = 0
        sections = []
        for original in (1, 2, 0):
            controls = sorted(top.grid_slaves(row=original),
                              key=lambda w: int(w.grid_info()["column"]))
            # Collect all rows before moving widgets into their final positions.
            sections.append(controls)
        workspace, chat, model = sections
        for widget in [*chat[2:4], *workspace[:2], *chat[:2]]:
            widget.grid(row=row, column=0, sticky="ew", padx=2, pady=1)
            row += 1
        actions = ttk.Frame(top)
        actions.grid(row=row, column=0, sticky="ew", pady=(2, 5))
        for column, widget in enumerate(chat[4:]):
            actions.columnconfigure(column, weight=1)
            widget.grid(in_=actions, row=0, column=column, sticky="ew", padx=2)
            widget.configure(style="Sidebar.TButton")
            widget.lift()
        row += 1
        for title, widgets in (("Manage workspace", workspace[2:]),
                               ("Model settings", model)):
            body = ttk.Frame(top)
            button = ttk.Button(top, style="SidebarSection.TButton")
            button.grid(row=row, column=0, sticky="ew", pady=(3, 0))
            row += 1
            body.grid(row=row, column=0, sticky="ew")
            body.columnconfigure(0, weight=1)
            row += 1
            for index, widget in enumerate(widgets):
                widget.grid(in_=body, row=index, column=0, sticky="ew", padx=2, pady=1)
                widget.lift()
            self.disclosure(button, body, title, expanded=False)
        top.columnconfigure(0, weight=1)

    @staticmethod
    def disclosure(button, body, title, expanded, on_change=None):
        def show(opened, save=False):
            if opened:
                body.grid()
            else:
                body.grid_remove()
            button.configure(text=("▾   " if opened else "▸   ") + title,
                             command=lambda: show(not opened, True))
            if save and on_change:
                on_change(opened)
        show(expanded)
