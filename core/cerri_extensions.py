"""Cerry plugin loader.  Install once before App().mainloop():  cerri_extensions.install(globals())

Reads  ai_workspace_data/plugins.json   {"enabled": ["cerry_chat", "cerry_audio", ...]}
and calls install(ns) of every plugin module in that list.

* A name is looked up as a module: the alias table first (vision -> cerry_vision, ...), then the name itself, then
  cerry_<name> and cerri_<name>. Plugin modules are loaded from the "plugins" folder next to the app.
* A plugin may define  ORDER = <number>  (default 50).  Smaller numbers are installed first, equal numbers keep
  the order of the list.  Wrapping order matters: cerry_docs 5, cerry_core 10, cerry_routing 20, cerry_chat 90.
* One broken or missing plugin never stops Cerry or the other plugins.  Everything, including how many
  milliseconds each plugin needed to start, is written to ai_workspace_data/plugins.log.
* Put a "#" or "-" in front of a name to switch it off without deleting it from the list.
* "Already installed" marks: every plugin marks itself on the App class (for example "_cm_installed") so that it is
  not installed twice.  Two plugins that picked the SAME mark used to block each other silently (cerry_models and
  cerry_contextmenu: the right-click menu and the input-box resizing disappeared).  The loader now clears a mark
  that another plugin has set before it installs the next plugin, and says so in plugins.log.
* The sidebar lays out built-in tool groups after plugins have finished adding controls.
  cerry_dock.py handles older plugins that still create independent toolbar rows.
"""
import importlib
import json
import re
import sys
import time
import traceback
from pathlib import Path

ALIASES = {"vision": "cerry_vision", "offline": "cerry_offline"}
DEFAULT_ORDER = 50
_INSTALLED = set()
FLAG_RE = re.compile(r"""["'](_\w*install\w*)["']""")


def candidates(name):
    out = []
    for c in (ALIASES.get(name), name, "cerry_" + name, "cerri_" + name):
        if c and c not in out:
            out.append(c)
    return out


def read_list(path):
    """Return (names, problem).  A missing file is created; a damaged file is reported, not fatal."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        try:
            path.write_text(json.dumps({"enabled": []}, indent=2), encoding="utf-8")
        except OSError:
            pass
        return [], "plugins.json did not exist - created an empty one"
    except (OSError, ValueError) as e:
        return [], f"plugins.json could not be read: {e}"
    raw = data.get("enabled", []) if isinstance(data, dict) else data
    names = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = item.strip()
            if item and not item.startswith(("#", "-")) and item not in names:
                names.append(item)
    return names, ""


def find_module(name):
    """Return (module, module_name) or (None, None) when no candidate file exists."""
    for cand in candidates(name):
        try:
            return importlib.import_module(cand), cand
        except ModuleNotFoundError as e:
            if e.name == cand:          # this candidate does not exist: try the next one
                continue
            raise                        # the plugin exists but one of ITS imports is missing (e.g. pillow)
    return None, None


def install_marks(mod):
    """The 'already installed' marks a plugin uses on the App class, found in its source file."""
    try:
        return set(FLAG_RE.findall(Path(mod.__file__).read_text(encoding="utf-8", errors="ignore")))
    except Exception:
        return set()


def load_plugins(ns, names):
    """Import and install the plugins.  Returns a list of (name, status, detail)."""
    App = ns["App"]
    results, ready = [], []
    for index, name in enumerate(names):
        t0 = time.perf_counter()
        try:
            mod, modname = find_module(name)
        except Exception as e:
            results.append((name, "error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
            continue
        if mod is None:
            results.append((name, "not found", "no module: " + ", ".join(candidates(name))))
            continue
        ready.append((getattr(mod, "ORDER", DEFAULT_ORDER), index, name, modname, mod,
                      (time.perf_counter() - t0) * 1000))
    for _, _, name, modname, mod, import_ms in sorted(ready, key=lambda t: (t[0], t[1])):
        if not callable(getattr(mod, "install", None)):
            results.append((name, "error", f"{modname} has no install(ns) function"))
            continue
        cleared = sorted(m for m in install_marks(mod) if m in App.__dict__)
        for mark in cleared:                  # set by another plugin that picked the same name
            try:
                delattr(App, mark)
            except AttributeError:
                pass
        t0 = time.perf_counter()
        try:
            mod.install(ns)
            ms = import_ms + (time.perf_counter() - t0) * 1000
            note = f" - mark {', '.join(cleared)} was already set by another plugin and was cleared" if cleared else ""
            results.append((name, "ok", f"{modname} ({ms:.0f} ms){note}"))
        except Exception as e:
            results.append((name, "error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
    return results


def write_log(path, names, problem, results, extra=()):
    lines = [time.strftime("Cerry plugin log - %Y-%m-%d %H:%M:%S"), f"list: {names}"]
    if problem:
        lines.append("note: " + problem)
    lines += [f"{status.upper():9} {name}: {detail}" for name, status, detail in results]
    lines += list(extra)
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def summary(results):
    ok = [n for n, s, _ in results if s == "ok"]
    bad = [(n, s, d.splitlines()[0] if d else "") for n, s, d in results if s != "ok"]
    return ok, bad


def import_dock():
    try:
        return importlib.import_module("cerry_dock")
    except Exception:
        return None


def install(ns):
    App = ns["App"]
    App.defer_sidebar_layout = True
    if App in _INSTALLED:
        return
    _INSTALLED.add(App)
    data_dir = ns["DATA_DIR"]
    app_dir = ns.get("APP_DIR")
    if app_dir:
        folder = Path(app_dir) / "plugins"
        if folder.is_dir() and str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    dock = import_dock()
    if dock is not None:
        App.__init__ = dock.wrap_base(App.__init__)       # innermost: remembers the main window's own widgets
    names, problem = read_list(data_dir / "plugins.json")
    results = load_plugins(ns, names)
    ok, bad = summary(results)
    original_init = App.__init__
    state = {"rows": 0, "dock_error": ""}

    def init(app, *args, **kwargs):
        original_init(app, *args, **kwargs)
        app.sidebar.finish_tools()
        if dock is not None:
            try:
                state["rows"] = dock.arrange(app, data_dir / "dock.json")
            except Exception as e:
                state["dock_error"] = f"{type(e).__name__}: {e}"
        write_log(data_dir / "plugins.log", names, problem, results,
                  [f"DOCK      rows moved into tabs: {state['rows']}" + (f" ({state['dock_error']})" if state["dock_error"] else "")])

        def report():
            try:
                text = f"Plugins loaded: {len(ok)}"
                if bad or problem:
                    text += f" - {len(bad)} with problems (see ai_workspace_data/plugins.log)"
                app.status.set(text)
                if bad or problem:
                    lines = [f"- {n}: {s} ({d[:120]})" for n, s, d in bad] + ([problem] if problem else [])
                    app.show("Plugin notes:\n" + "\n".join(lines))
            except Exception:
                pass
        app.after(1200, report)

    App.__init__ = init
