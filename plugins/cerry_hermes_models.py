"""Cerry Hermes models: the Hermes Agent window uses the same model list as the main Cerry window.

Why Hermes showed old models: the Hermes plugin builds its list from the program's built-in model list. Models you
change under "Manage models..." live somewhere else, so Hermes never saw them. This plugin reads the list from the
main window's Model box (the one you see and use), so it works however Manage models stores the models.

What it does
 - Hermes tasks offer the models of the main Model box, in the same order, and re-read it every time you open the
   Hermes window or the task editor, so new, renamed and removed models appear without restarting.
 - Providers that Hermes knows: gemini, groq, openrouter and nvidia (added by this plugin; Hermes has an "nvidia"
   provider that uses NVIDIA_API_KEY). Models of other providers, for example AnythingLLM, are skipped, and the
   reason is written to ai_workspace_data/hermes_models.log (rewritten whenever the list changes) and shown in the
   status line. Extra providers can be added in ai_workspace_data/hermes.json:
   "provider_slugs": {"myprovider": "its-hermes-name"}
 - NVIDIA models need NVIDIA_API_KEY. Cerry passes the keys from key.env to a Hermes gateway that Cerry starts itself.
   If you start the gateway yourself, put NVIDIA_API_KEY into Hermes' own .env file as well.
 - A task saved with models that no longer exist falls back to the default chain (the model selected in the main window
   first, then the others, local models last). The confirmation window shows the models before anything runs.
Enable by adding "cerry_hermes_models" to ai_workspace_data/plugins.json (anywhere after "cerry_hermes").
Needs cerry_hermes_core.py, which you already have.
"""
import importlib
import time
import traceback
from pathlib import Path

ORDER = 150
DEFAULT_SLUGS = {'nvidia': 'nvidia'}
state = {'app': None, 'ns': None, 'skipped': [], 'signature': None}


def find_combobox(app):
    variable = str(getattr(app, 'model_var', ''))
    if not variable:
        return None
    stack = [app]
    while stack:
        widget = stack.pop()
        for child in widget.winfo_children():
            try:
                if child.winfo_class() == 'TCombobox' and str(child.cget('textvariable')) == variable:
                    return child
            except Exception:
                pass
            stack.append(child)
    return None


def live_models(app, ns, fallback):
    """Models in the main window's Model box, as dictionaries with label, provider and id."""
    box = find_combobox(app)
    if box is None:
        return list(fallback)
    labels = [str(x) for x in app.tk.splitlist(box.cget('values'))]
    if not labels:
        return list(fallback)
    known = {}
    for source in (ns.get('MODELS') or [], fallback):
        for m in source:
            known.setdefault(m.get('label'), m)
    models, original = [], app.model_var.get()
    for label in labels:
        model = known.get(label)
        if model is None:
            try:
                app.model_var.set(label)
                candidate = app.selected_model()
                if candidate and candidate.get('label') == label:
                    model = candidate
            except Exception:
                model = None
            finally:
                app.model_var.set(original)
        if model is not None:
            models.append(model)
    return models or list(fallback)


def log_text(live, routes, slugs):
    usable = {r['label']: r for r in routes}
    lines = ['Hermes model list - %s' % time.strftime('%Y-%m-%d %H:%M:%S'),
             'Source: the Model box of the main window (%d models). Hermes providers: %s' % (
                 len(live), ', '.join('%s -> %s' % kv for kv in sorted(slugs.items()))), '']
    lines.append('%-3s %-36s %-14s %-22s %s' % ('#', 'Label', 'Provider', 'Hermes', 'Status'))
    skipped = []
    for n, m in enumerate(live, 1):
        label, provider = str(m.get('label')), str(m.get('provider'))
        if label in usable:
            lines.append('%-3d %-36s %-14s %-22s %s' % (n, label[:36], provider[:14], usable[label]['provider'][:22], 'usable'))
        else:
            skipped.append((label, "provider '%s' is not known to Hermes" % provider))
            lines.append('%-3d %-36s %-14s %-22s %s' % (n, label[:36], provider[:14], '-', 'SKIPPED'))
    locals_ = [r for r in routes if r.get('local')]
    for r in locals_:
        lines.append('%-3s %-36s %-14s %-22s %s' % ('+', r['label'][:36], 'local', r['provider'][:22], 'usable (from hermes.json local_models)'))
    lines += ['', 'Usable: %d   Skipped: %d' % (len(routes), len(skipped))]
    for label, reason in skipped:
        lines.append('  - %s: %s' % (label, reason))
    return '\n'.join(lines) + '\n', skipped


def install(ns):
    core = importlib.import_module('cerry_hermes_core')
    if getattr(core, '_hm_installed', False):
        return
    core._hm_installed = True
    state['ns'] = ns
    data_dir = Path(ns['DATA_DIR'])
    original_routes, original_order = core.build_routes, core.order_chain

    def write_log(text):
        try:
            (data_dir / 'hermes_models.log').write_text(text, encoding='utf-8')
        except OSError:
            pass

    def build_routes(models, cfg):
        app = state['app']
        live = models
        if app is not None:
            try:
                live = live_models(app, ns, models)
            except Exception:
                write_log('could not read the main model list, using the built-in list:\n' + traceback.format_exc())
        slugs = dict(DEFAULT_SLUGS)
        slugs.update(cfg.get('provider_slugs', {}))
        merged = dict(cfg, provider_slugs=slugs)
        routes = original_routes(live, merged)
        text, skipped = log_text(live, routes, slugs)
        state['skipped'] = skipped
        signature = tuple((m.get('label'), m.get('provider'), m.get('id')) for m in live) + tuple(sorted(slugs.items()))
        if signature != state['signature']:
            state['signature'] = signature
            write_log(text)
        return routes

    def order_chain(labels, routes):
        chain = original_order(labels, routes)
        if chain or not routes:
            return chain
        selected = None
        try:
            selected = state['app'].selected_model()['label']
        except Exception:
            pass
        return original_order(core.default_chain(routes, selected), routes)

    core.build_routes, core.order_chain = build_routes, order_chain

    App = ns['App']
    original_init = App.__init__

    def init(app, *args, **kwargs):
        state['app'] = app
        original_init(app, *args, **kwargs)
        state['app'] = app
    App.__init__ = init

    def announce(method):
        def wrapper(agent, *a, **k):
            try:
                routes = agent.routes
                if state['skipped']:
                    agent.app.status.set('Hermes models: %d usable, %d skipped (see hermes_models.log)' % (
                        len(routes), len(state['skipped'])))
                else:
                    agent.app.status.set('Hermes models: %d, same list as the main window' % len(routes))
            except Exception:
                pass
            return method(agent, *a, **k)
        return wrapper

    for module_name in ('cerry_hermes', 'cerry_hermes1'):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        agent = getattr(module, 'Agent', None)
        if agent is not None and not getattr(agent, '_hm_wrapped', False):
            for name in ('panel', 'editor'):
                if hasattr(agent, name):
                    setattr(agent, name, announce(getattr(agent, name)))
            agent._hm_wrapped = True
