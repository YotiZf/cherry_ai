"""Cerry chat text size: A- / A+ buttons, Ctrl+mouse wheel, Ctrl+Plus / Ctrl+Minus / Ctrl+0.
Enable by adding "cerry_textsize" to ai_workspace_data/plugins.json. The size is remembered.
It scales the chat window and the input box together, keeping fonts, bold and italic styles.
"""
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont

ORDER = 90
LOW, HIGH = -3, 14


def chat_widget(app):
    return getattr(app, 'chat_w', None) or getattr(app, 'chatw', None)


def install(ns):
    App = ns['App']
    if App.__dict__.get('_textsize_installed'):
        return
    App._textsize_installed = True
    store = Path(ns['DATA_DIR']) / 'text_size.json'
    try:
        start = int(json.loads(store.read_text(encoding='utf-8')).get('delta', 0))
    except (OSError, ValueError, AttributeError):
        start = 0
    start = max(LOW, min(HIGH, start))
    original_init = App.__init__

    def spec_of(app, widget, tag=None):
        try:
            raw = widget.tag_cget(tag, 'font') if tag else widget.cget('font')
        except tk.TclError:
            return None
        if not raw:
            return None
        info = tkfont.Font(root=app, font=raw).actual()
        return {'family': info['family'], 'size': abs(int(info['size'])) or 10,
                'bold': info['weight'] == 'bold', 'italic': info['slant'] == 'italic',
                'underline': bool(info['underline'])}

    def build(base, delta):
        styles = [s for s, on in (('bold', base['bold']), ('italic', base['italic']),
                                  ('underline', base['underline'])) if on]
        return (base['family'], max(6, base['size'] + delta), *styles)

    def apply(app):
        chat = chat_widget(app)
        delta = app._text_delta
        for widget in (chat, getattr(app, 'inp', None)):
            if widget is None:
                continue
            bases = app._text_bases.setdefault(str(widget), {})
            names = [None] + (list(widget.tag_names()) if widget is chat else [])
            for name in names:
                if name not in bases:
                    spec = spec_of(app, widget, name)
                    if spec is None:
                        continue
                    bases[name] = spec
                font = build(bases[name], delta)
                try:
                    if name is None:
                        widget.configure(font=font)
                    else:
                        widget.tag_configure(name, font=font)
                except tk.TclError:
                    pass
        app._text_label.set(('%+d' % delta) if delta else 'default')
        try:
            store.write_text(json.dumps({'delta': delta}), encoding='utf-8')
        except OSError:
            pass

    def change(app, step):
        value = 0 if step is None else app._text_delta + step
        app._text_delta = max(LOW, min(HIGH, value))
        apply(app)

    def init(app, *args, **kwargs):
        original_init(app, *args, **kwargs)
        chat = chat_widget(app)
        if chat is None:
            print('cerry_textsize: chat window not found, plugin skipped')
            return
        app._text_delta = start
        app._text_bases = {}
        app._text_label = tk.StringVar(value='default')
        bar = ttk.Frame(app, padding=(6, 2))
        try:
            bar.pack(fill='x', before=chat.frame)
        except (tk.TclError, AttributeError):
            bar.pack(fill='x')
        ttk.Label(bar, text='Text size:').pack(side='left')
        ttk.Button(bar, text='A-', width=3, command=lambda: change(app, -1)).pack(side='left', padx=2)
        ttk.Button(bar, text='A+', width=3, command=lambda: change(app, 1)).pack(side='left', padx=2)
        ttk.Button(bar, text='Reset', width=6, command=lambda: change(app, None)).pack(side='left', padx=2)
        ttk.Label(bar, textvariable=app._text_label, width=8).pack(side='left', padx=6)

        def wheel(event):
            change(app, 1 if event.delta > 0 else -1)
            return 'break'
        for widget in (chat, app.inp):
            widget.bind('<Control-MouseWheel>', wheel)
            widget.bind('<Control-Button-4>', lambda e: (change(app, 1), 'break')[1])
            widget.bind('<Control-Button-5>', lambda e: (change(app, -1), 'break')[1])
        for key, step in (('<Control-plus>', 1), ('<Control-equal>', 1), ('<Control-KP_Add>', 1),
                          ('<Control-minus>', -1), ('<Control-KP_Subtract>', -1), ('<Control-Key-0>', None)):
            app.bind_all(key, lambda e, s=step: (change(app, s), 'break')[1])
        app.after(300, lambda: apply(app))
    App.__init__ = init
