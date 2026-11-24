"""Cerry model manager: add, delete, disable and re-order models from a window - no code editing.

Switch on:  add "cerry_models" to ai_workspace_data/plugins.json  (put it FIRST in the list, before the other plugins)
Use:        "Manage models..." button next to "List models" (or type  /models )
Saved in:   ai_workspace_data/models.json   (this file is now the master list; the MODELS list inside the main
            script is only used the very first time, and by "Restore defaults")

What you can do in the window
  * Up / Down / Top        move the selected model. The list order is the fallback order: the model chosen in the main
                           window answers first; if it fails or is too slow, Cerry tries the others from top to bottom
                           (local models always last). Untick "Use this order for fallback" to keep Routing's own order.
  * Add / Edit / Delete    change the list. "List from provider" asks the provider which model IDs it offers.
  * Enable / Disable       keep a model in the list but do not use it (for example while it is rate-limited).
  * Test / Test all        send a tiny request and show whether the model answers and how fast.
  * Providers...           add or edit a provider (name, base URL, name of the key in key.env), for example NVIDIA.
  * Use now                make the selected model the active one in the main window.
First start: the four NVIDIA models are added once (provider "nvidia", key NVIDIA_API_KEY). Delete them if you do not
want them - they will not come back (only "Restore defaults" re-adds them).
"""
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import tkinter as tk
from tkinter import ttk, messagebox

INSTALL_FLAG = "_cm_installed"
SEED_ID = "nvidia4"
NVIDIA_PROVIDER = {"url": "https://integrate.api.nvidia.com/v1", "env": "NVIDIA_API_KEY", "local": False}
NVIDIA_MODELS = [
    {"label": "NVIDIA Llama 3.3 70B", "provider": "nvidia", "id": "meta/llama-3.3-70b-instruct"},
    {"label": "NVIDIA DeepSeek V4 Pro", "provider": "nvidia", "id": "deepseek-ai/deepseek-v4-pro"},
    {"label": "NVIDIA Mistral Medium 3.5", "provider": "nvidia", "id": "mistralai/mistral-medium-3-5-128b"},
    {"label": "NVIDIA Nemotron 3 Super 120B", "provider": "nvidia", "id": "nvidia/nemotron-3-super-120b-a12b"},
]
INFO = ("Order = fallback order. The model chosen in the main window answers first; if it fails or is too slow, Cerry "
        "tries the models below it from top to bottom (local models always last). Disabled models stay in the list "
        "but are never used. Changes apply immediately to the next request.")


def walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from walk(child)


def short(text, n=140):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= n else text[: n - 1] + "..."


class Manager:
    def __init__(self, ns):
        self.ns = ns
        self.models = ns["MODELS"]
        self.provs = ns["PROVIDERS"]
        self.routing = ns.get("routing")
        self.file = Path(ns["DATA_DIR"]) / "models.json"
        self.code_models = [dict(m) for m in self.models]
        self.code_provs = {k: tuple(v) for k, v in self.provs.items()}
        self.base_local = set(getattr(self.routing, "LOCAL_PROVIDERS", ()) or ())
        self._orig_local = getattr(self.routing, "LOCAL_PROVIDERS", None)
        self.providers, self.entries, self.seeded = {}, [], set()
        self.use_order = True
        self.app = None
        self.window = None
        self.tests = {}
        self.load()
        self.apply()

    # ------------------------------------------------------------ storage
    def code_defaults(self):
        provs = {n: {"url": u, "env": e, "local": n in self.base_local} for n, (u, e) in self.code_provs.items()}
        entries = [dict(m, enabled=True) for m in self.code_models]
        return provs, entries

    def load(self):
        data = None
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.providers, self.entries = self.code_defaults()
        self.seeded = set()
        if isinstance(data, dict) and isinstance(data.get("models"), list):
            for n, p in (data.get("providers") or {}).items():
                if isinstance(p, dict) and p.get("url") and (p.get("env") or n == "ollama"):
                    self.providers[n] = {"url": p["url"], "env": p.get("env", ""), "local": bool(p.get("local"))}
            self.entries = [dict(m, enabled=bool(m.get("enabled", True))) for m in data["models"]
                            if isinstance(m, dict) and m.get("label") and m.get("provider") and m.get("id")]
            self.seeded = set(data.get("seeded", []))
            self.use_order = bool(data.get("use_order", True))
        if SEED_ID not in self.seeded:
            self.seed_nvidia()
            self.seeded.add(SEED_ID)
            self.save()
        elif not self.file.exists():
            self.save()

    def seed_nvidia(self):
        self.providers.setdefault("nvidia", dict(NVIDIA_PROVIDER))
        have = {(e["provider"], e["id"]) for e in self.entries}
        labels = {e["label"] for e in self.entries}
        local = self.local_names()
        at = next((i for i, e in enumerate(self.entries) if e["provider"] in local), len(self.entries))
        for m in NVIDIA_MODELS:
            if (m["provider"], m["id"]) in have or m["label"] in labels:
                continue
            self.entries.insert(at, dict(m, enabled=True))
            at += 1

    def save(self):
        data = {"version": 1, "providers": self.providers, "models": self.entries,
                "seeded": sorted(self.seeded), "use_order": self.use_order}
        tmp = self.file.with_name(self.file.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.file)

    def local_names(self):
        return set(self.base_local) | {n for n, p in self.providers.items() if p.get("local")}

    # ------------------------------------------------------------ applying to Cerry (in place!)
    def apply(self, save=True):
        live = [{k: v for k, v in e.items() if k != "enabled"} for e in self.entries if e.get("enabled", True)]
        self.models[:] = live
        for name, p in self.providers.items():
            self.provs[name] = (p["url"], p["env"])
        for name in list(self.provs):
            if name not in self.providers:
                del self.provs[name]
        if self.routing is not None and self._orig_local is not None:
            want = self.local_names()
            if set(getattr(self.routing, "LOCAL_PROVIDERS", ()) or ()) != want:
                try:
                    self.routing.LOCAL_PROVIDERS = type(self._orig_local)(sorted(want))
                except Exception:
                    self.routing.LOCAL_PROVIDERS = frozenset(want)
        if save:
            self.save()
        self.refresh_main()

    def reload_keys(self):
        try:
            from dotenv import load_dotenv
            base = self.ns.get("APP_DIR")
            paths = ([Path(base) / "key.env", Path(base) / ".env"] if base else []) + [Path("key.env")]
            for p in paths:
                if p.exists():
                    load_dotenv(p, override=False)
        except Exception:
            pass

    def drop_client(self, provider):
        cache = self.ns.get("_clients")
        if isinstance(cache, dict):
            cache.pop(provider, None)

    def key_ok(self, provider):
        p = self.providers.get(provider)
        return bool(p and (os.getenv(p["env"]) if p["env"] else provider == "ollama"))

    # ------------------------------------------------------------ list operations
    def find(self, label):
        return next((i for i, e in enumerate(self.entries) if e["label"] == label), None)

    def move(self, i, delta):
        j = i + delta
        if 0 <= i < len(self.entries) and 0 <= j < len(self.entries):
            self.entries[i], self.entries[j] = self.entries[j], self.entries[i]
            self.apply()
            return j
        return i

    def move_top(self, i):
        if 0 < i < len(self.entries):
            self.entries.insert(0, self.entries.pop(i))
            self.apply()
            return 0
        return i

    def check_entry(self, label, provider, mid, skip=None):
        label, mid = label.strip(), mid.strip()
        if not label or len(label) > 70:
            raise ValueError("Give the model a name (up to 70 characters).")
        if provider not in self.providers:
            raise ValueError("Choose a provider (or add one with New provider).")
        if not mid or re.search(r"\s", mid):
            raise ValueError("The model ID is required and cannot contain spaces.")
        for i, e in enumerate(self.entries):
            if i != skip and e["label"].lower() == label.lower():
                raise ValueError(f"A model called '{e['label']}' already exists. Names must be unique.")
        return label, mid

    def add_entry(self, label, provider, mid):
        label, mid = self.check_entry(label, provider, mid)
        local = self.local_names()
        at = next((i for i, e in enumerate(self.entries) if e["provider"] in local), len(self.entries))
        if provider in local:
            at = len(self.entries)
        self.entries.insert(at, {"label": label, "provider": provider, "id": mid, "enabled": True})
        self.apply()
        return at

    def update_entry(self, i, label, provider, mid):
        label, mid = self.check_entry(label, provider, mid, skip=i)
        old = self.entries[i]["label"]
        self.entries[i].update(label=label, provider=provider, id=mid)
        if self.app is not None and label != old:
            try:
                if self.app.model_var.get() == old:
                    self.app.model_var.set(label)
            except Exception:
                pass
        self.apply()

    def delete_entry(self, i):
        if len([e for e in self.entries if e.get("enabled", True)]) <= 1 and self.entries[i].get("enabled", True):
            raise ValueError("Keep at least one enabled model.")
        del self.entries[i]
        self.apply()

    def toggle_entry(self, i):
        e = self.entries[i]
        if e.get("enabled", True) and len([x for x in self.entries if x.get("enabled", True)]) <= 1:
            raise ValueError("Keep at least one enabled model.")
        e["enabled"] = not e.get("enabled", True)
        self.apply()
        return e["enabled"]

    def restore_defaults(self):
        self.providers, self.entries = self.code_defaults()
        self.seed_nvidia()
        self.seeded.add(SEED_ID)
        self.tests.clear()
        self.apply()

    # ------------------------------------------------------------ providers
    def check_provider(self, name, url, env, editing=None):
        name, url, env = name.strip().lower(), url.strip(), env.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,23}", name):
            raise ValueError("Provider name: 2-24 letters, digits, - or _ (for example nvidia).")
        if name in self.providers and name != editing:
            raise ValueError(f"The provider '{name}' already exists.")
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("Base URL must start with https:// (or http://localhost for a local server).")
        if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Plain http is only allowed for localhost. Use https:// for online providers.")
        if (env or name != "ollama") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
            raise ValueError("Key name in key.env: letters, digits and _ only (for example NVIDIA_API_KEY).")
        return name, url, env

    def save_provider(self, name, url, env, local, editing=None):
        name, url, env = self.check_provider(name, url, env, editing)
        if editing and editing != name:
            raise ValueError("A provider cannot be renamed. Add a new one and delete the old one.")
        self.providers[name] = {"url": url, "env": env, "local": bool(local)}
        self.drop_client(name)
        self.reload_keys()
        self.apply()
        return name

    def delete_provider(self, name):
        if any(e["provider"] == name for e in self.entries):
            raise ValueError("Models still use this provider. Delete or edit those models first.")
        self.providers.pop(name, None)
        self.drop_client(name)
        self.apply()

    # ------------------------------------------------------------ main-window integration
    def main_combobox(self):
        app = self.app
        if app is None:
            return None
        try:
            var = str(app.model_var)
            for w in walk(app):
                if isinstance(w, ttk.Combobox) and str(w.cget("textvariable")) == var:
                    return w
        except Exception:
            pass
        return None

    def refresh_main(self):
        app = self.app
        if app is None:
            return
        labels = [m["label"] for m in self.models]
        cb = self.main_combobox()
        try:
            if cb is not None:
                cb.configure(values=labels)
            if labels and app.model_var.get() not in labels:
                app.model_var.set(labels[0])
        except Exception:
            pass

    # ------------------------------------------------------------ network helpers (run in a thread)
    def test_model(self, e):
        self.reload_keys()
        t0 = time.time()
        try:
            client = self.ns["get_client"](e["provider"])
            try:
                client = client.with_options(timeout=45, max_retries=0)
            except Exception:
                pass
            r = client.chat.completions.create(
                model=e["id"], messages=[{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=24)
            text = ""
            try:
                text = (r.choices[0].message.content or "").strip()
            except Exception:
                pass
            return f"ok {time.time() - t0:.1f}s" + (f" ({short(text, 20)})" if text else "")
        except Exception as ex:
            return "error: " + short(ex, 110)

    def list_remote(self, provider):
        self.reload_keys()
        client = self.ns["get_client"](provider)
        ids = sorted({re.sub(r"^models/", "", str(m.id)) for m in client.models.list()})
        return ids

    def run_bg(self, fn, done):
        box = {}

        def work():
            try:
                box["r"] = fn()
            except Exception as ex:   # noqa
                box["e"] = ex
            box["done"] = True
        threading.Thread(target=work, daemon=True).start()

        def poll():
            if box.get("done"):
                try:
                    done(box.get("r"), box.get("e"))
                except tk.TclError:
                    pass
            else:
                self.app.after(150, poll)
        self.app.after(150, poll)

    # ------------------------------------------------------------ the window
    def alive(self):
        try:
            return self.window is not None and self.window.winfo_exists()
        except tk.TclError:
            return False

    def open(self):
        if self.alive():
            self.window.deiconify()
            self.window.lift()
            self.fill()
            return
        win = tk.Toplevel(self.app)
        self.window = win
        win.title("Cerry - Models")
        win.geometry("1000x600")
        ttk.Label(win, text=INFO, wraplength=960, padding=8).pack(fill="x")
        cols = ("pos", "on", "label", "provider", "id", "key", "test")
        widths = (40, 40, 240, 100, 290, 60, 190)
        self.tree = ttk.Treeview(win, columns=cols, show="headings", selectmode="browse", height=14)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text={"pos": "#", "on": "On", "id": "Model ID", "key": "Key", "test": "Last test"
                                       }.get(c, c.title()))
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8)
        self.tree.tag_configure("off", foreground="#999999")
        self.tree.tag_configure("bad", foreground="#b00020")
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())
        self.tree.bind("<Alt-Up>", lambda e: self.move_selected(-1))
        self.tree.bind("<Alt-Down>", lambda e: self.move_selected(1))
        r1 = ttk.Frame(win, padding=(8, 6, 8, 0))
        r1.pack(fill="x")
        for text, fn in (("Up", lambda: self.move_selected(-1)), ("Down", lambda: self.move_selected(1)),
                         ("Top", self.top_selected), ("Add...", self.add_dialog), ("Edit...", self.edit_selected),
                         ("Delete", self.delete_selected), ("Enable / Disable", self.toggle_selected)):
            ttk.Button(r1, text=text, command=fn).pack(side="left", padx=3)
        r2 = ttk.Frame(win, padding=(8, 4, 8, 0))
        r2.pack(fill="x")
        for text, fn in (("Test", self.test_selected), ("Test all", self.test_all), ("Use now", self.use_now),
                         ("Providers...", self.providers_dialog), ("Restore defaults", self.restore),
                         ("Close", win.destroy)):
            ttk.Button(r2, text=text, command=fn).pack(side="left", padx=3)
        ttk.Button(r2, text="Scan keyed providers...", command=self.scan_providers).pack(side="left", padx=3)
        self.order_var = tk.BooleanVar(value=self.use_order)
        ttk.Checkbutton(win, text="Use this order for fallback (otherwise Routing's own order is kept)",
                        variable=self.order_var, command=self.order_changed).pack(anchor="w", padx=10, pady=4)
        self.info_var = tk.StringVar(value="Select a model, then use the buttons. Alt+Up / Alt+Down also move it.")
        ttk.Label(win, textvariable=self.info_var, style="Muted.TLabel", wraplength=960).pack(fill="x", padx=10, pady=(0, 8))
        self.fill()

    def order_changed(self):
        self.use_order = bool(self.order_var.get())
        self.save()

    def say(self, text):
        if self.alive():
            self.info_var.set(text)

    def fill(self, select=None):
        if not self.alive():
            return
        self.tree.delete(*self.tree.get_children())
        local = self.local_names()
        for i, e in enumerate(self.entries):
            on = e.get("enabled", True)
            key = "ok" if self.key_ok(e["provider"]) else "missing"
            res = self.tests.get(e["label"], "")
            tags = ("off",) if not on else (("bad",) if res.startswith("error") else ())
            prov = e["provider"] + (" (local)" if e["provider"] in local else "")
            self.tree.insert("", "end", iid=str(i), tags=tags,
                             values=(i + 1, "yes" if on else "no", e["label"], prov, e["id"], key, res))
        if select is not None and 0 <= select < len(self.entries):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    def sel(self):
        if not self.alive():
            return None
        s = self.tree.selection()
        return int(s[0]) if s else None

    def need_sel(self):
        i = self.sel()
        if i is None:
            self.say("Select a model first.")
        return i

    def guard(self, fn, *a):
        try:
            return fn(*a)
        except ValueError as ex:
            messagebox.showerror("Models", str(ex), parent=self.window)
        return None

    def move_selected(self, d):
        i = self.need_sel()
        if i is not None:
            self.fill(select=self.move(i, d))
        return "break"

    def top_selected(self):
        i = self.need_sel()
        if i is not None:
            self.fill(select=self.move_top(i))

    def delete_selected(self):
        i = self.need_sel()
        if i is None:
            return
        e = self.entries[i]
        if not messagebox.askyesno("Delete model", f"Delete '{e['label']}' ({e['provider']}: {e['id']}) from the list?",
                                   parent=self.window):
            return
        try:
            self.delete_entry(i)
        except ValueError as ex:
            messagebox.showerror("Models", str(ex), parent=self.window)
            return
        self.tests.pop(e["label"], None)
        self.fill(select=min(i, len(self.entries) - 1))

    def toggle_selected(self):
        i = self.need_sel()
        if i is not None:
            self.guard(self.toggle_entry, i)
            self.fill(select=i)

    def use_now(self):
        i = self.need_sel()
        if i is None:
            return
        e = self.entries[i]
        if not e.get("enabled", True):
            self.say("This model is disabled. Enable it first.")
            return
        try:
            self.app.model_var.set(e["label"])
            self.say(f"'{e['label']}' is now the active model in the main window.")
        except Exception as ex:
            self.say(f"Could not select it: {ex}")

    def restore(self):
        if messagebox.askyesno("Restore defaults", "Replace the whole list with the models and providers from the main "
                               "script, plus the four NVIDIA models? Your own additions will be removed.",
                               parent=self.window):
            self.restore_defaults()
            self.fill(select=0)

    def run_tests(self, idxs):
        entries = [dict(self.entries[i]) for i in idxs]
        if not entries:
            return
        self.say(f"Testing {len(entries)} model(s) ...")

        def job():
            return [(e["label"], self.test_model(e)) for e in entries]

        def done(res, err):
            if err:
                self.say(f"Test failed: {short(err)}")
                return
            for label, text in res:
                self.tests[label] = text
            self.fill(select=self.sel())
            self.say("Test finished. A red 'error' row usually means a wrong model ID, a missing key, or no access "
                     "to that model with your account.")
        self.run_bg(job, done)

    def test_selected(self):
        i = self.need_sel()
        if i is not None:
            self.run_tests([i])

    def test_all(self):
        self.run_tests([i for i, e in enumerate(self.entries) if e.get("enabled", True)])

    def scan_providers(self):
        """Discover provider catalogs and make a tiny live request to every returned model.

        This is opt-in because successful requests may consume provider quota or incur cost.
        Results are diagnostic only and never change the configured model list.
        """
        self.reload_keys()
        providers = [(n, dict(p)) for n, p in self.providers.items()
                     if self.key_ok(n)]
        if not providers:
            self.say("No providers are ready to scan.")
            return
        if not messagebox.askyesno(
                "Scan providers",
                "Cherry will list models for every configured provider, then send a tiny test prompt "
                "to each returned model. This may use API quota or incur small charges. Continue?",
                parent=self.window):
            return
        self.say(f"Discovering and testing models from {len(providers)} provider(s)...")

        def job():
            rows = []
            for provider, _cfg in providers:
                try:
                    ids = self.list_remote(provider)
                except Exception as ex:
                    rows.append((provider, "", "provider error: " + short(ex, 160)))
                    continue
                for model_id in ids:
                    result = self.test_model({"provider": provider, "id": model_id})
                    rows.append((provider, model_id, result))
            return rows

        def done(rows, err):
            if err:
                self.say("Provider scan failed: " + short(err, 160))
                return
            available = [(p, mid) for p, mid, result in rows if result.startswith("ok ")]
            self.tests.update({f"{p}:{mid}": result for p, mid, result in rows})
            report = ["Provider model scan", "Available means a live test prompt returned successfully.", ""]
            report.extend(f"{'AVAILABLE' if result.startswith('ok ') else 'UNAVAILABLE':11} {p}: {mid or '(provider)'} — {result}"
                          for p, mid, result in rows)
            report += ["", f"{len(available)} available of {sum(bool(mid) for _, mid, _ in rows)} discovered model(s)."]
            path = Path(self.ns["DATA_DIR"]) / "provider_scan.txt"
            try:
                path.write_text("\n".join(report) + "\n", encoding="utf-8")
            except OSError:
                pass
            self.say(f"Scan complete: {len(available)} models available. Full results: {path.name}")
            messagebox.showinfo("Provider model scan", "\n".join(report[:24]) +
                                ("\n...\nSee ai_workspace_data/provider_scan.txt for all results." if len(report) > 24 else ""),
                                parent=self.window)
        self.run_bg(job, done)

    # ------------------------------------------------------------ add / edit dialogs
    def add_dialog(self):
        self.entry_dialog(None)

    def edit_selected(self):
        i = self.sel()
        if i is not None:
            self.entry_dialog(i)

    def entry_dialog(self, idx):
        e = self.entries[idx] if idx is not None else {"label": "", "provider": "", "id": ""}
        dlg = tk.Toplevel(self.window if self.alive() else self.app)
        dlg.title("Edit model" if idx is not None else "Add model")
        dlg.transient(self.window if self.alive() else self.app)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        label = tk.StringVar(value=e["label"])
        prov = tk.StringVar(value=e["provider"] or next(iter(self.providers), ""))
        mid = tk.StringVar(value=e["id"])
        ttk.Label(frm, text="Name shown in Cerry").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=label, width=52).grid(row=0, column=1, columnspan=2, sticky="we", padx=6)
        ttk.Label(frm, text="Provider").grid(row=1, column=0, sticky="w", pady=3)
        cb = ttk.Combobox(frm, textvariable=prov, values=list(self.providers), state="readonly", width=24)
        cb.grid(row=1, column=1, sticky="w", padx=6)

        def new_provider():
            def made(name):
                cb.configure(values=list(self.providers))
                prov.set(name)
            self.provider_form(None, made, parent=dlg)
        ttk.Button(frm, text="New provider...", command=new_provider).grid(row=1, column=2, sticky="w")
        ttk.Label(frm, text="Model ID").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=mid, width=52).grid(row=2, column=1, sticky="we", padx=6)

        def pick_remote():
            p = prov.get()
            if not p:
                return
            note.set("Asking the provider for its model list ...")

            def done(ids, err):
                if err:
                    note.set("Could not get the list: " + short(err, 120))
                    return
                note.set(f"{len(ids)} IDs received. The provider's list can be incomplete - you can still type an ID.")
                self.pick_dialog(ids, lambda v: mid.set(v), dlg)
            self.run_bg(lambda: self.list_remote(p), done)
        ttk.Button(frm, text="List from provider", command=pick_remote).grid(row=2, column=2, sticky="w")
        note = tk.StringVar(value="Copy the exact ID from the provider's model page, for example "
                                  "meta/llama-3.3-70b-instruct.")
        ttk.Label(frm, textvariable=note, style="Muted.TLabel", wraplength=560).grid(row=3, column=0, columnspan=3,
                                                                                       sticky="w", pady=6)

        def ok():
            try:
                if idx is None:
                    pos = self.add_entry(label.get(), prov.get(), mid.get())
                else:
                    self.update_entry(idx, label.get(), prov.get(), mid.get())
                    pos = idx
            except ValueError as ex:
                messagebox.showerror("Models", str(ex), parent=dlg)
                return
            dlg.destroy()
            self.fill(select=pos)
            self.say("Saved. Use Test to check that it answers.")
        bar = ttk.Frame(frm)
        bar.grid(row=4, column=0, columnspan=3, sticky="e")
        ttk.Button(bar, text="Save", command=ok).pack(side="left", padx=4)
        ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)

    def pick_dialog(self, ids, choose, parent):
        dlg = tk.Toplevel(parent)
        dlg.title("Models offered by the provider")
        dlg.transient(parent)
        dlg.geometry("560x520")
        flt = tk.StringVar()
        ttk.Entry(dlg, textvariable=flt).pack(fill="x", padx=8, pady=6)
        box = tk.Listbox(dlg, exportselection=False)
        box.pack(fill="both", expand=True, padx=8)

        def refill(*_):
            box.delete(0, "end")
            q = flt.get().lower()
            for i in ids:
                if q in i.lower():
                    box.insert("end", i)
        flt.trace_add("write", refill)
        refill()

        def take(*_):
            s = box.curselection()
            if s:
                choose(box.get(s[0]))
                dlg.destroy()
        box.bind("<Double-1>", take)
        ttk.Button(dlg, text="Use selected", command=take).pack(pady=6)

    # ------------------------------------------------------------ providers
    def providers_dialog(self):
        dlg = tk.Toplevel(self.window if self.alive() else self.app)
        dlg.title("Providers")
        dlg.geometry("820x340")
        dlg.transient(self.window if self.alive() else self.app)
        ttk.Label(dlg, padding=8, wraplength=780, text=(
            "A provider is a service with an OpenAI-compatible API. The key itself stays in key.env under the name "
            "shown; new key lines are picked up without restarting.")).pack(fill="x")
        cols = ("name", "url", "env", "key", "local")
        tv = ttk.Treeview(dlg, columns=cols, show="headings", selectmode="browse", height=8)
        for c, w in zip(cols, (110, 330, 170, 70, 60)):
            tv.heading(c, text=c.title() if c != "env" else "Key name")
            tv.column(c, width=w)
        tv.pack(fill="both", expand=True, padx=8)

        def fill_p():
            tv.delete(*tv.get_children())
            for n, p in self.providers.items():
                tv.insert("", "end", iid=n, values=(n, p["url"], p["env"], ("optional" if not p["env"] else "ok") if self.key_ok(n) else "missing",
                                                     "yes" if n in self.local_names() else ""))

        def cur():
            s = tv.selection()
            return s[0] if s else None

        def add():
            self.provider_form(None, lambda n: fill_p(), parent=dlg)

        def edit():
            n = cur()
            if n:
                self.provider_form(n, lambda n2: fill_p(), parent=dlg)

        def delete():
            n = cur()
            if n and messagebox.askyesno("Delete provider", f"Delete provider '{n}'?", parent=dlg):
                try:
                    self.delete_provider(n)
                except ValueError as ex:
                    messagebox.showerror("Providers", str(ex), parent=dlg)
                fill_p()
                self.fill()
        bar = ttk.Frame(dlg, padding=8)
        bar.pack(fill="x")
        for text, fn in (("Add...", add), ("Edit...", edit), ("Delete", delete), ("Close", dlg.destroy)):
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=3)
        fill_p()

    def provider_form(self, name, made, parent):
        p = self.providers.get(name) if name else {"url": "https://", "env": "", "local": False}
        dlg = tk.Toplevel(parent)
        dlg.title("Edit provider" if name else "New provider")
        dlg.transient(parent)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both", expand=True)
        v_name, v_url = tk.StringVar(value=name or ""), tk.StringVar(value=p["url"])
        v_env, v_local = tk.StringVar(value=p["env"]), tk.BooleanVar(value=name in self.local_names() if name else False)
        rows = (("Name (lowercase)", v_name, "nvidia"),
                ("Base URL", v_url, "https://integrate.api.nvidia.com/v1"),
                ("Key name in key.env", v_env, "NVIDIA_API_KEY"))
        for r, (text, var, hint) in enumerate(rows):
            ttk.Label(frm, text=text).grid(row=r, column=0, sticky="w", pady=3)
            ent = ttk.Entry(frm, textvariable=var, width=56)
            ent.grid(row=r, column=1, sticky="we", padx=6)
            if name and r == 0:
                ent.configure(state="disabled")
            ttk.Label(frm, text="e.g. " + hint, style="Muted.TLabel").grid(row=r, column=2, sticky="w")
        ttk.Checkbutton(frm, text="This provider runs on my own computer (counts as local / offline-safe)",
                        variable=v_local).grid(row=3, column=0, columnspan=3, sticky="w", pady=6)

        def ok():
            try:
                n = self.save_provider(name or v_name.get(), v_url.get(), v_env.get(), v_local.get(), editing=name)
            except ValueError as ex:
                messagebox.showerror("Provider", str(ex), parent=dlg)
                return
            dlg.destroy()
            made(n)
            self.fill()
        bar = ttk.Frame(frm)
        bar.grid(row=4, column=0, columnspan=3, sticky="e")
        ttk.Button(bar, text="Save", command=ok).pack(side="left", padx=4)
        ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)

    # ------------------------------------------------------------ attach to the main window
    def attach(self, app):
        self.app = app
        self.refresh_main()
        placed = False
        for w in walk(app):
            try:
                if isinstance(w, ttk.Button) and w.cget("text") == "List models":
                    parent = w.master
                    cols = [int(s.grid_info().get("column", 0)) for s in parent.grid_slaves(row=0)]
                    ttk.Button(parent, text="Manage models...", command=self.open).grid(row=0, column=max(cols) + 1, padx=2)
                    placed = True
                    break
            except (tk.TclError, ValueError):
                continue
        if not placed:
            strip = app.sidebar.tool_row("Workspace settings")
            ttk.Button(strip, text="Manage models...", command=self.open).pack(side="left", padx=2)


def install(ns):
    App = ns["App"]
    if App.__dict__.get(INSTALL_FLAG):
        return
    setattr(App, INSTALL_FLAG, True)
    mgr = Manager(ns)
    ns["_model_manager"] = mgr

    routing = ns.get("routing")
    original_build = getattr(routing, "build_order", None)
    if original_build is not None:
        def build_order(models, selected, policy, *a, **k):
            res = original_build(models, selected, policy, *a, **k)
            try:
                if not mgr.use_order:
                    return res
                order = list(res[0])
                if len(order) > 2:
                    rank = {m["label"]: i for i, m in enumerate(ns["MODELS"])}
                    local = set(getattr(routing, "LOCAL_PROVIDERS", ()) or ())
                    tail = sorted(order[1:], key=lambda m: (m.get("provider") in local, rank.get(m.get("label"), 10 ** 6)))
                    return (order[:1] + tail,) + tuple(res[1:])
            except Exception:
                pass
            return res
        routing.build_order = build_order

    original_init, original_send = App.__init__, App.send

    def init(app, *a, **k):
        original_init(app, *a, **k)
        mgr.attach(app)

    def send(app):
        text = app.inp.get("1.0", "end").strip()
        if text.lower() in ("/models", "/model"):
            app.inp.delete("1.0", "end")
            mgr.open()
            return
        return original_send(app)

    App.__init__, App.send = init, send
