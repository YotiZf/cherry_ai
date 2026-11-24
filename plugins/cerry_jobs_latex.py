"""Cerry Jobs output: CV as LaTeX (.tex), cover letter as PDF, plus the /joblatex window.
Replace cerry_jobs_latex.py. Keep jobs_latex_engine.py and cerry_export.py installed.
Keep this plugin in plugins.json AFTER your Jobs plugin (cerry_jobs_1 or cerry_jobs).
If something fails, details are written to ai_workspace_data/jobs_latex_error.log.
The settings window can be opened by typing /joblatex then Enter, with the Jobs LaTeX button above the chat,
or with the Send button.
"""
import importlib
import json
import os
import time
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk

ORDER = 80
COMMANDS = ('/joblatex', '/job latex', '/job-latex')


def chat_widget(app):
    return getattr(app, 'chat_w', None) or getattr(app, 'chatw', None)


def install(ns):
    App = ns['App']
    if App.__dict__.get('_jobs_latex_v4'):
        return
    App._jobs_latex_v4 = True
    data_dir = Path(ns['DATA_DIR'])
    data_dir.mkdir(parents=True, exist_ok=True)
    log_file = data_dir / 'jobs_latex_error.log'
    settings = data_dir / 'jobs_latex.json'
    cfg = {'enabled': True}
    try:
        saved = json.loads(settings.read_text(encoding='utf-8'))
        if isinstance(saved, dict):
            cfg.update(saved)
    except (OSError, ValueError):
        pass
    state = {'hooked': False, 'message': 'Jobs plugin not found yet.'}

    def note(text):
        with log_file.open('a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + text + '\n')

    def find_jobs():
        for name in ('cerry_jobs_1', 'cerry_jobs'):
            try:
                module = importlib.import_module(name)
            except ModuleNotFoundError as error:
                if error.name == name:
                    continue
                note(name + ' import failed: ' + traceback.format_exc())
                continue
            except Exception:
                note(name + ' import failed: ' + traceback.format_exc())
                continue
            if hasattr(module, 'Hunter') and hasattr(module.Hunter, 'save_docs'):
                return name, module
        return None, None

    def write_pdf(markdown, path):
        exporter = importlib.import_module('cerry_export')
        exporter.build_pdf(markdown, path, 'Cover letter')

    def hook_jobs():
        if state['hooked']:
            return True
        name, module = find_jobs()
        if module is None:
            state['message'] = 'Jobs plugin not found. Check its name in plugins.json.'
            return False
        original = module.Hunter.save_docs

        def save_docs(hunter, folder, stem, markdown):
            if not cfg['enabled'] or stem not in ('CV_tailored', 'CoverLetter'):
                return original(hunter, folder, stem, markdown)
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)
            side = folder / 'markdown'
            side.mkdir(exist_ok=True)
            (side / (stem + '.md')).write_text(markdown, encoding='utf-8')
            try:
                if stem == 'CV_tailored':
                    engine = importlib.import_module('jobs_latex_engine')
                    target = folder / 'CV_tailored.tex'
                    temporary = folder / 'CV_tailored.tex.tmp'
                    temporary.write_text(engine.document(markdown, 'cv'), encoding='utf-8')
                    temporary.replace(target)
                else:
                    target = folder / 'CoverLetter.pdf'
                    temporary = folder / 'CoverLetter.build.pdf'
                    write_pdf(markdown, temporary)
                    if not temporary.exists() or temporary.stat().st_size == 0:
                        raise RuntimeError('PDF file was not created.')
                    temporary.replace(target)
                hunter.log('  Saved ' + str(target))
            except Exception as error:
                note(stem + ' export failed: ' + traceback.format_exc())
                hunter.log('  ' + stem + ' export failed (' + str(error) + '). Editable text is in ' + str(side))
        module.Hunter.save_docs = save_docs
        state['hooked'] = True
        state['message'] = 'Active, hooked into ' + name + '.'
        return True

    def is_command(text):
        return ' '.join(text.strip().lower().split()) in COMMANDS

    def panel(app):
        old = getattr(app, '_jobs_latex_win', None)
        try:
            if old is not None and old.winfo_exists():
                old.deiconify()
                old.lift()
                old.focus_force()
                return
        except tk.TclError:
            pass
        hook_jobs()
        win = tk.Toplevel(app)
        app._jobs_latex_win = win
        win.title('Jobs - CV LaTeX / cover letter PDF')
        win.transient(app)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill='both', expand=True)
        enabled = tk.BooleanVar(value=bool(cfg['enabled']))
        ttk.Checkbutton(frame, text='CV as LaTeX (.tex) and cover letter as PDF', variable=enabled).pack(anchor='w')
        ttk.Label(frame, wraplength=560, justify='left', text=(
            'When enabled, newly generated job documents are saved as CV_tailored.tex and CoverLetter.pdf. '
            'The CV is not compiled automatically; open it in TeXstudio and run pdfLaTeX. '
            'Your source CV is set where you already set it for /job; its format does not change the output. '
            'Editable Markdown copies are kept in each job folder under markdown/.')).pack(fill='x', pady=8)
        ttk.Label(frame, wraplength=560, text='Status: ' + state['message']).pack(anchor='w', pady=4)

        def open_log():
            if log_file.exists() and hasattr(os, 'startfile'):
                os.startfile(str(log_file))
        ttk.Button(frame, text='Open error log', command=open_log).pack(anchor='w', pady=2)

        def save():
            cfg['enabled'] = bool(enabled.get())
            settings.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
            app.status.set('Jobs output saved: CV .tex, cover letter PDF.' if cfg['enabled'] else 'Jobs output: LaTeX/PDF mode disabled.')
            win.destroy()
        ttk.Button(frame, text='Save', command=save).pack(anchor='e', pady=6)
        win.lift()
        win.focus_force()

    original_init, original_send = App.__init__, App.send

    def init(app, *args, **kwargs):
        original_init(app, *args, **kwargs)
        try:
            hook_jobs()
            bar = app.sidebar.tool_row("Jobs")
            ttk.Button(bar, text='Jobs LaTeX...', command=lambda: panel(app)).pack(side='left')
            tag = 'JobsLatexCommand'
            app.inp.bindtags((tag,) + tuple(app.inp.bindtags()))

            def on_return(event):
                if event.state & 0x1:
                    return None
                if is_command(app.inp.get('1.0', 'end')):
                    app.inp.delete('1.0', 'end')
                    panel(app)
                    return 'break'
                return None
            app.bind_class(tag, '<Return>', on_return)
            app.after(1500, hook_jobs)
        except Exception:
            note('init failed: ' + traceback.format_exc())

    def send(app):
        try:
            if is_command(app.inp.get('1.0', 'end')):
                app.inp.delete('1.0', 'end')
                panel(app)
                return
        except Exception:
            note('send hook failed: ' + traceback.format_exc())
        return original_send(app)
    App.__init__, App.send = init, send
    try:
        hook_jobs()
    except Exception:
        note('install failed: ' + traceback.format_exc())
