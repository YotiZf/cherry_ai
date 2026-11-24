"""Unlink an extra workspace directory without deleting any files.
Enable cerry_unlink in plugins.json. Use Unlink folder or /unlink.
Previously quoted text in chats, cached extractions and saved memories are not erased.
"""
import tkinter as tk
from tkinter import ttk, messagebox

ORDER = 95


def install(ns):
    import doc_manager
    App = ns['App']
    if App.__dict__.get('_workspace_unlink_plugin_installed'):
        return
    original_init, original_send = App.__init__, App.send
    original_reindex = App.reindex_async
    original_search = doc_manager.DocumentIndex.search
    original_active = doc_manager.DocumentIndex.active_files

    def excluded_for(index, excluded):
        owner = getattr(index, '_unlink_owner', None)
        if owner is not None and not owner.cfg.get('folder'):
            # Guard against a pre-unlink background scan completing with its old sources.
            stale = {r.key for r in index.records if r.label == 'linked'}
            return set(excluded) | stale
        return excluded

    def search(index, query, k=6, excluded=()):
        return original_search(index, query, k, excluded_for(index, excluded))

    def active(index, excluded=()):
        return original_active(index, excluded_for(index, excluded))

    def reindex(app):
        app.ws.index._unlink_owner = app.ws
        return original_reindex(app)

    def unlink(app):
        if getattr(app, 'busy', False):
            messagebox.showinfo('Unlink folder', 'Finish or stop the current answer first.', parent=app)
            return
        ws = app.ws
        folder = ws.cfg.get('folder', '')
        if not folder:
            app.status.set('This workspace has no extra folder linked.')
            return
        if not messagebox.askyesno('Unlink folder',
                f'Unlink this directory from workspace {ws.name}?\n\n{folder}\n\n'
                'No source files will be deleted. Existing chats, memories and cached text are retained.', parent=app):
            return
        ws.cfg['folder'] = ''
        try:
            ws.save_cfg()
        except Exception:
            ws.cfg['folder'] = folder
            raise
        ws.index._unlink_owner = ws
        app.reindex_async()
        app.status.set('Extra folder unlinked. Refreshing workspace documents; no files deleted.')

    def init(app, *args, **kwargs):
        original_init(app, *args, **kwargs)
        app.ws.index._unlink_owner = app.ws
        parent = app.ws_box.master
        columns = [int(w.grid_info().get('column', 0)) for w in parent.grid_slaves(row=1)]
        ttk.Button(parent, text='Unlink folder', command=lambda: unlink(app)).grid(
            row=1, column=max(columns, default=0) + 1, padx=2)
        app._unlink_folder = lambda: unlink(app)

    def send(app):
        if app.inp.get('1.0', 'end').strip().lower() == '/unlink':
            app.inp.delete('1.0', 'end')
            unlink(app)
            return
        return original_send(app)

    doc_manager.DocumentIndex.search = search
    doc_manager.DocumentIndex.active_files = active
    App.__init__, App.send, App.reindex_async = init, send, reindex
    App._workspace_unlink_plugin_installed = True
