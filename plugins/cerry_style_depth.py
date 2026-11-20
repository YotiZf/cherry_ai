"""Extend Cerry's writing-style plugin: 0-8 examples per letter and folder-study depth.
Enable cerry_style_depth in plugins.json alongside cerry_style. No existing source file is replaced.
Requires the Style class and style_engine supplied with cerry_style.

New installations default to 3 examples; existing saved choices are preserved.
Quick: up to 6 balanced documents, original bounded single-call analysis.
Standard: up to 15 balanced documents, original bounded single-call analysis.
Deep: up to 30 documents in batches of 5, then combine the batch guides.
Thorough: up to 60 documents in batches of 5, then combine the batch guides.
Only supported readable files count. Each is still limited by style_engine.MAX_CHARS.
These are analysis limits, not a claim that every byte of every document is read.
Settings are kept in the existing ai_workspace_data/style.json.
"""
import importlib
import json
import time
import tkinter as tk
from tkinter import ttk

ORDER = 75
LEVELS = ('Quick', 'Standard', 'Deep', 'Thorough')
LIMITS = {'Quick': 6, 'Standard': 15, 'Deep': 30, 'Thorough': 60}
DESCRIPTIONS = {
    'Quick': 'Up to 6 documents; one bounded model call.',
    'Standard': 'Up to 15 documents; one bounded model call, excerpts only.',
    'Deep': 'Up to 30 documents in batches, then combine guides; more tokens and time.',
    'Thorough': 'Up to 60 documents in batches, then combine guides; highest token use.',
}


def balanced(samples, limit):
    """Round-robin across document type and language instead of taking only first filenames."""
    groups = {}
    for sample in samples:
        groups.setdefault((sample.get('kind', 'other'), sample.get('lang', 'en')), []).append(sample)
    out = []
    while groups and len(out) < limit:
        for key in list(groups):
            if len(out) >= limit:
                break
            out.append(groups[key].pop(0))
            if not groups[key]:
                del groups[key]
    return out


def combine(guides, keys, ask):
    prompt = ('Combine these writing-style observations about ONE writer into one coherent guide. '
        'Preserve language-specific differences. Do not invent personal facts. '
        'Return ONLY a JSON object with plain string values for these keys: ' + ', '.join(keys) +
        '. Each language/CV guide must be at most 200 words. Consolidate duplicates. '
        'If observations conflict, favour recurring habits and mention uncertainty.\n\n' +
        json.dumps([{k: g.get(k, '') for k in keys} for g in guides], ensure_ascii=False))
    text = ask('You consolidate writing-style guides. Return JSON only, without file tags.', prompt)
    decoder = json.JSONDecoder()
    data = None
    for i, character in enumerate(text):
        if character == '{':
            try:
                data, _ = decoder.raw_decode(text[i:])
                break
            except ValueError:
                pass
    if not isinstance(data, dict) or not any(isinstance(data.get(k), str) and data[k].strip() for k in keys):
        raise ValueError('The model did not return a usable combined style guide. Existing saved guide is unchanged.')
    return {k: data.get(k, '').strip() if isinstance(data.get(k), str) else '' for k in keys}


def analyse(samples, ask, folder, depth, original, keys):
    depth = depth if depth in LEVELS else 'Standard'
    chosen = balanced(samples, LIMITS[depth])
    if not chosen:
        raise ValueError('No readable writing samples found.')
    batches = [chosen] if depth in ('Quick', 'Standard') else [chosen[i:i+5] for i in range(0, len(chosen), 5)]
    guides = [original(batch, ask, folder) for batch in batches]
    profile = dict(guides[0])
    if len(guides) > 1:
        profile.update(combine(guides, keys, ask))
    profile['files'] = list(dict.fromkeys(name for guide in guides for name in guide.get('files', [])))
    profile['counts'] = {
        'letters': sum(s.get('kind') == 'letter' for s in chosen),
        'cvs': sum(s.get('kind') == 'cv' for s in chosen),
        'other': sum(s.get('kind') not in ('letter', 'cv') for s in chosen),
    }
    profile.update(study_depth=depth, selected_documents=len(chosen), analysis_batches=len(batches),
                   built=time.strftime('%Y-%m-%d %H:%M'), edited=False)
    return profile


def walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from walk(child)


def install(ns):
    App = ns['App']
    if App.__dict__.get('_writing_depth_extension_installed'):
        return
    style = importlib.import_module('cerry_style')
    if not App.__dict__.get('_style_plugin_installed'):
        style.install(ns)
    engine = style.se
    style.DEFAULTS['examples'] = 3
    style.DEFAULTS.setdefault('study_depth', 'Standard')
    holder = {}
    original_build = engine.build_profile

    def build_profile(samples, ask, folder=''):
        owner = holder.get('owner')
        depth = owner.cfg.get('study_depth', 'Standard') if owner else 'Standard'
        return analyse(samples, ask, folder, depth, original_build, engine.KEYS)
    engine.build_profile = build_profile
    original_ensure = style.Style.ensure_profile

    def ensure_profile(owner, ask, log=None):
        profile = owner.profile()
        depth = owner.cfg.get('study_depth', 'Standard')
        if profile and not profile.get('edited') and profile.get('study_depth', 'Standard') != depth:
            if log:
                log('Re-studying writing samples at depth ' + depth + ' ...')
            profile = engine.build_profile(owner.samples(), ask, owner.cfg.get('folder', ''))
            owner.save_profile(profile)
            return profile
        return original_ensure(owner, ask, log)
    style.Style.ensure_profile = ensure_profile
    original_open = style.Style.open

    def open_window(owner):
        original_open(owner)
        # Upgrade the existing examples dropdown, rather than creating a second setting.
        target = str(owner.ex_var)
        for widget in walk(owner.window):
            try:
                if widget.winfo_class() == 'TCombobox' and str(widget.cget('textvariable')) == target:
                    widget.configure(values=[str(i) for i in range(9)])
            except tk.TclError:
                pass
        try:
            if owner._writing_depth_row.winfo_exists():
                return
        except (AttributeError, tk.TclError):
            pass
        row = owner._writing_depth_row = ttk.Frame(owner.window, padding=8)
        row.pack(fill='x')
        ttk.Label(row, text='Folder-study depth:').pack(side='left')
        value = tk.StringVar(value=owner.cfg.get('study_depth', 'Standard'))
        hint = tk.StringVar(value=DESCRIPTIONS.get(value.get(), DESCRIPTIONS['Standard']))
        box = ttk.Combobox(row, textvariable=value, values=LEVELS, state='readonly', width=11)
        box.pack(side='left', padx=6)
        ttk.Label(row, textvariable=hint, wraplength=480).pack(side='left', padx=6)

        def changed(_event=None):
            owner.cfg['study_depth'] = value.get()
            owner.save_cfg()
            hint.set(DESCRIPTIONS[value.get()])
            owner.set_status('Study depth saved. Press Study folder now to rebuild; this replaces any manual guide edits.')
        box.bind('<<ComboboxSelected>>', changed)
        # Keep Tk variables alive for the lifetime of the window.
        owner._writing_depth_vars = (value, hint)
    style.Style.open = open_window
    original_init = App.__init__

    def init(app, *args, **kwargs):
        original_init(app, *args, **kwargs)
        owner = app._style
        owner.cfg.setdefault('study_depth', 'Standard')
        holder['owner'] = owner
    App.__init__ = init
    App._writing_depth_extension_installed = True
