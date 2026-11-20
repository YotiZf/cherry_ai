# -*- coding: utf-8 -*-
"""
Created on Thu Oct 29 14:51:50 2026

@author: panaj
"""

import json
import tkinter as tk
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from tkinter import ttk

LOCAL_PROVIDERS = {"anythingllm"}

ERROR_KINDS = {
    "config": "key missing in key.env",
    "auth": "invalid key or no permission (401/403)",
    "rate_limit": "rate limit or quota reached (429)",
    "not_found": "model or workspace not found (404)",
    "timeout": "request timed out",
    "connection": "cannot connect (app closed, no internet)",
    "server": "provider server error (5xx)",
    "bad_request": "request rejected (e.g. input too long)",
    "unknown": "any other error",
}


class ConfigError(RuntimeError):
    """Raised when an API key is missing."""


def is_local(model):
    return model["provider"] in LOCAL_PROVIDERS


@dataclass
class RoutingPolicy:
    local_only: bool = False
    allow_fallback: bool = True
    allowed_providers: list = field(default_factory=lambda: ["gemini", "groq", "openrouter", "anythingllm"])
    fallback_on: list = field(default_factory=lambda: list(ERROR_KINDS))

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            names = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in names})
        except Exception:
            return cls()

    def save(self, path):
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def build_order(models, selected, policy):
    """Return (ordered_models, notes). Local-only mode never includes a cloud model."""
    notes = []
    if policy.local_only:
        local = [m for m in models if is_local(m)]
        if not local:
            raise RuntimeError("Local-only mode is on, but no local model is configured.")
        first = selected if is_local(selected) else local[0]
        if first is not selected:
            notes.append(f"Local-only: using {first['label']} instead of {selected['label']}.")
        order = [first]
        if policy.allow_fallback:
            order += [m for m in local if m is not first]
        return order, notes

    order = [selected]
    if policy.allow_fallback:
        order += [m for m in models
                  if m is not selected and m["provider"] in policy.allowed_providers]
    return order, notes


def classify_error(exc):
    if isinstance(exc, ConfigError):
        return "config"
    if isinstance(exc, TimeoutError):      # also raised by the response-deadline plugin
        return "timeout"
    try:
        import openai
    except ImportError:
        return "unknown"
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return "auth"
    if isinstance(exc, openai.RateLimitError):
        return "rate_limit"
    if isinstance(exc, openai.NotFoundError):
        return "not_found"
    if isinstance(exc, openai.APITimeoutError):
        return "timeout"
    if isinstance(exc, openai.APIConnectionError):
        return "connection"
    if isinstance(exc, openai.InternalServerError):
        return "server"
    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        return "bad_request"
    return "unknown"


def should_fallback(kind, policy):
    return policy.allow_fallback and kind in policy.fallback_on


def open_routing_dialog(parent, policy, path, provider_names, on_change=None):
    win = tk.Toplevel(parent)
    win.title("Routing and privacy")
    win.transient(parent)
    frm = ttk.Frame(win, padding=12)
    frm.pack(fill="both", expand=True)

    local_var = tk.BooleanVar(value=policy.local_only)
    fb_var = tk.BooleanVar(value=policy.allow_fallback)
    ttk.Checkbutton(frm, text="Local only (nothing goes to cloud providers or web search)",
                    variable=local_var).pack(anchor="w")
    ttk.Checkbutton(frm, text="Allow fallback to other models", variable=fb_var).pack(anchor="w", pady=(6, 0))

    ttk.Label(frm, text="Providers allowed as fallback:").pack(anchor="w", pady=(10, 2))
    prov_vars = {}
    for name in provider_names:
        v = tk.BooleanVar(value=name in policy.allowed_providers)
        prov_vars[name] = v
        tag = "local" if name in LOCAL_PROVIDERS else "cloud"
        ttk.Checkbutton(frm, text=f"{name} ({tag})", variable=v).pack(anchor="w", padx=16)

    ttk.Label(frm, text="Fall back after these errors:").pack(anchor="w", pady=(10, 2))
    kind_vars = {}
    for kind, desc in ERROR_KINDS.items():
        v = tk.BooleanVar(value=kind in policy.fallback_on)
        kind_vars[kind] = v
        ttk.Checkbutton(frm, text=f"{kind} - {desc}", variable=v).pack(anchor="w", padx=16)

    def save():
        policy.local_only = local_var.get()
        policy.allow_fallback = fb_var.get()
        policy.allowed_providers = [n for n, v in prov_vars.items() if v.get()]
        policy.fallback_on = [k for k, v in kind_vars.items() if v.get()]
        policy.save(path)
        if on_change:
            on_change()
        win.destroy()

    row = ttk.Frame(frm)
    row.pack(fill="x", pady=(12, 0))
    ttk.Button(row, text="Save", command=save).pack(side="right")
    ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right", padx=6)