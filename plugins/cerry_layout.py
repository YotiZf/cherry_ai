"""Responsive Cerry toolbar. Full replacement for cerry_layout.py.
Keeps the original layout at normal width. Combines toolbar rows when maximized
or 20% wider than the opening width. Restores the exact original root pack order.
Enable cerry_layout in plugins.json. No additional packages required.
Settings: ai_workspace_data/layout.json; diagnostics: layout.log.
"""
import json
import time
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk

ORDER = 200


def lines_for(widths, available, gap):
    lines, line, used = [], [], 0
    for index, width in enumerate(widths):
        if line and used + width + gap > available:
            lines.append(line)
            line, used = [], 0
        line.append(index)
        used += width + gap
    if line:
        lines.append(line)
    return lines


def install(ns):
    App = ns['App']
    if App.__dict__.get('_layout_installed'):
        return
    App._layout_installed = True
    data = Path(ns['DATA_DIR'])
    cfg = dict(enabled=True, wide_ratio=1.2, wide_from=0, gap=4, max_spread=48)
    try:
        saved = json.loads((data / 'layout.json').read_text(encoding='utf-8'))
        if isinstance(saved, dict):
            cfg.update(saved)
    except (OSError, ValueError):
        pass

    def log(text):
        try:
            path = data / 'layout.log'
            if path.exists() and path.stat().st_size > 200000:
                path.write_text('', encoding='utf-8')
            with path.open('a', encoding='utf-8') as f:
                f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + text + '\n')
        except OSError:
            pass

    def tlist(app, result):
        return list(result) if isinstance(result, (tuple, list)) else list(app.tk.splitlist(result))

    def pack_paths(app):
        return [str(x) for x in tlist(app, app.tk.call('pack', 'slaves', str(app)))]

    def pack_info(app, path):
        values = tlist(app, app.tk.call('pack', 'info', path))
        return {str(values[i]): values[i+1] for i in range(0, len(values), 2)}

    def exists(app, path):
        return bool(int(app.tk.call('winfo', 'exists', str(path))))

    def raw_pack(app, path, options):
        # Use Tcl paths, not ScrolledText's delegated pack method.
        args = []
        for k, v in options.items():
            if k not in ('-before', '-after'):
                args.extend((k, v))
        app.tk.call('pack', 'configure', str(path), *args)

    def capture(app, st):
        paths = pack_paths(app)
        chat = getattr(app, 'chat_w', None)
        if chat is None:
            chat = getattr(app, 'chatw', None)
        if chat is None:
            return False
        anchor = str(getattr(chat, 'frame', chat))
        if anchor not in paths:
            return False
        st['root'] = [(p, pack_info(app, p)) for p in paths]
        st['anchor'] = anchor
        frames = []
        for path in paths[:paths.index(anchor)]:
            if str(app.tk.call('winfo', 'class', path)) not in ('TFrame', 'Frame'):
                continue
            frame = app.nametowidget(path)
            if frame.winfo_children():
                frames.append(frame)
        if not frames:
            return False
        # All original controls remain alive; no commands/variables are recreated.
        records, chunks = [], []
        for frame in frames:
            grid = []
            packed = []
            for w in frame.winfo_children():
                manager = w.winfo_manager()
                if manager == 'grid':
                    info = dict(w.grid_info())
                    grid.append((int(info['row']), int(info['column']), w, info))
                elif manager == 'pack':
                    packed.append(w)
            if grid:
                row_items = {}
                for r, c, w, info in sorted(grid, key=lambda x: (x[0], x[1])):
                    records.append((w, 'grid', info))
                    row_items.setdefault(r, []).append(w)
                for row in row_items.values():
                    i = 0
                    while i < len(row):
                        if row[i].winfo_class() in ('TLabel', 'Label') and i+1 < len(row):
                            chunks.append(row[i:i+2]); i += 2
                        else:
                            chunks.append([row[i]]); i += 1
            elif packed:
                # Preserve internal pack order, including labels beside their controls.
                row = list(frame.pack_slaves())
                for w in row:
                    records.append((w, 'pack', dict(w.pack_info())))
                # These controls have a common parent, so retain this complete bar as a chunk.
                chunks.append([frame])
        if not records:
            return False
        st.update(frames=frames, records=records, chunks=chunks)
        return True

    def restore_order(app, st):
        # Rebuild the COMPLETE original sibling order, not just toolbar placement.
        # Otherwise restoring individual toolbars can leave the expanding answer frame first.
        original = [(p, o) for p, o in st['root'] if exists(app, p)]
        known = {p for p, _ in original}
        extras = [(p, pack_info(app, p)) for p in pack_paths(app) if p not in known]
        for p in pack_paths(app):
            app.tk.call('pack', 'forget', p)
        for p, options in original + extras:
            raw_pack(app, p, options)

    def restore(app, st):
        if not st['wide']:
            return
        # Unmanage widgets before removing temporary holders. Never destroy originals.
        for chunk in st['chunks']:
            for w in chunk:
                if w.winfo_exists():
                    w.pack_forget()
        for holder in st['holders']:
            holder.destroy()
        st['holders'] = []
        for w, manager, info in st['records']:
            if not w.winfo_exists():
                continue
            options = dict(info)
            if 'in' in options:
                options['in_'] = options.pop('in')
            if manager == 'grid':
                w.grid(**options)
            else:
                w.pack(**options)
        restore_order(app, st)
        st['wide'] = False
        st['signature'] = None
        log('restored original layout and complete root order')

    def arrange(app, st, width):
        gap = max(0, int(cfg['gap']))
        widths = [sum(w.winfo_reqwidth() for w in c) + gap * (len(c)-1) for c in st['chunks']]
        lines = lines_for(widths, width-20, gap)
        signature = (width, tuple(widths))
        if st['signature'] == signature:
            return
        if st['wide']:
            restore(app, st)
        st['wide'] = True
        st['signature'] = signature
        container = st['frames'][0]
        # Release all grid slaves before packing temporary row frames.
        for w, manager, _ in st['records']:
            if manager == 'grid':
                w.grid_forget()
        for frame in st['frames'][1:]:
            frame.pack_forget()
        for indices in lines:
            holder = ttk.Frame(container)
            holder.pack(fill='x', side='top')
            st['holders'].append(holder)
            free = max(0, width-20-sum(widths[i] for i in indices)-gap*len(indices))
            extra = min(int(cfg['max_spread']), free//max(1,len(indices)-1))
            for position, i in enumerate(indices):
                for w in st['chunks'][i]:
                    w.pack(in_=holder, side='left', padx=(0,gap), pady=1)
                    w.lift()
                if extra and position < len(indices)-1:
                    ttk.Frame(holder, width=extra, height=1).pack(side='left')
        log('responsive layout: width=%d lines=%d' % (width, len(lines)))

    def refresh(app):
        st = app._layout_state
        if st['busy'] or st['disabled'] or not cfg['enabled']:
            return
        st['busy'] = True
        try:
            width = app.winfo_width()
            if width < 300:
                return
            zoomed = app.state() == 'zoomed'
            if st['base'] is None and not zoomed:
                st['base'] = width
            large = zoomed or (int(cfg['wide_from']) > 0 and width >= int(cfg['wide_from'])) or (
                st['base'] is not None and width >= st['base'] * float(cfg['wide_ratio']))
            if large:
                if not st['wide'] and not capture(app, st):
                    return
                arrange(app, st, width)
            else:
                restore(app, st)
        except Exception:
            log(traceback.format_exc())
            try:
                restore(app, st)
            except Exception:
                log('restore failed: ' + traceback.format_exc())
            st['disabled'] = True
        finally:
            st['busy'] = False

    original_init = App.__init__

    def init(app, *a, **k):
        original_init(app, *a, **k)
        if hasattr(app, "sidebar"):
            return
        app._layout_state = dict(base=None, wide=False, holders=[], signature=None,
                                 root=[], frames=[], records=[], chunks=[], busy=False, disabled=False)
        app._layout_job = None

        def scheduled():
            app._layout_job = None
            refresh(app)

        def changed(event):
            if event.widget is not app:
                return
            if app._layout_job is not None:
                app.after_cancel(app._layout_job)
            app._layout_job = app.after(180, scheduled)

        def poll():
            refresh(app)
            app.after(1500, poll)
        app.bind('<Configure>', changed, add='+')
        app.after(800, poll)
        log('layout v7 ready: complete sibling-order restoration')
    App.__init__ = init
