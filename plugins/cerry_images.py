"""Cerry images: make pictures from a text description.

Default provider: Cloudflare Workers AI (FLUX.1 schnell) - has a free daily allowance.
Optional provider: Google "Nano Banana" (needs a Gemini API project with billing switched on).

Commands:   /image <what to draw>     /image help     /image provider cloudflare|gemini     /image models
Also the "🎨 Image..." button above the input box.

Switch it on by adding  "cerry_images"  to the list in  ai_workspace_data/plugins.json
Settings: ai_workspace_data/images.json      Keys go in key.env (see /image help)
"""
import base64
import json
import os
import queue
import re
import threading
import time
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import ttk, simpledialog, filedialog

import httpx

CF_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_LIST = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"
DEFAULTS = {
    "provider": "cloudflare",
    "cloudflare_model": "@cf/black-forest-labs/flux-1-schnell",
    "steps": 4,                      # FLUX schnell: 1-8; more steps = slower and uses more of the free allowance
    "gemini_models": ["gemini-3.1-flash-image", "gemini-3.1-flash-image-preview", "gemini-2.5-flash-image"],
    "daily_limit": 40,
}
HELP = """Pictures from a text description

/image <description>    create a picture, e.g.  /image a watercolour of an RNA splicing diagram
/image provider <name>  switch between  cloudflare  (default, free daily allowance)  and  gemini  (needs billing)
/image models           (gemini only) list the image models your key can use

Cloudflare setup (free, no card): in the Cloudflare dashboard open Workers AI > "Use REST API",
create the Workers AI API token and copy your Account ID. Add two lines to key.env:
    CLOUDFLARE_ACCOUNT_ID=...
    CLOUDFLARE_API_TOKEN=...
Then restart Cerry. Descriptions of up to 2048 characters work; English usually gives the best result.
Pictures are saved in the workspace's images folder."""


class ImageError(Exception):
    pass


def sniff(data):
    """Return (mime, extension) from the first bytes of an image."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return "image/png", ".png"


# ----------------------------------------------------------------------
# Cloudflare Workers AI
# ----------------------------------------------------------------------
def cloudflare_text(payload):
    errors = payload.get("errors") if isinstance(payload, dict) else None
    return "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in (errors or []))[:400]


def cloudflare_message(status, payload):
    text = cloudflare_text(payload)
    low = text.lower()
    if "neuron" in low or "daily free" in low or "allocation" in low:
        return ("Today's free Cloudflare allowance is used up. It resets daily (UTC). "
                f"Cloudflare said: {text}")
    if status in (401, 403) or "authentication" in low:
        return ("Cloudflare did not accept the key. Check CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in key.env "
                f"(the token needs Workers AI permission). Cloudflare said: {text or status}")
    if status == 429:
        return f"Too many requests - wait a moment and try again. {text}"
    if status == 404:
        return f"Cloudflare could not find that model or account. Check the Account ID and cloudflare_model. {text}"
    return f"Cloudflare returned error {status}. {text}"


def cloudflare_image(account, token, model, prompt, steps=4, post=httpx.post, timeout=120):
    """Return (bytes, mime, note, model_used). Only the settings Cloudflare accepts are sent."""
    if len(prompt) > 2048:
        raise ImageError(f"The description is {len(prompt)} characters long; Cloudflare accepts at most 2048.")
    body = {"prompt": prompt, "steps": max(1, min(8, int(steps)))}
    for _ in range(3):
        try:
            r = post(CF_URL.format(account=account, model=model), json=body,
                     headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        except httpx.HTTPError as e:
            raise ImageError(f"Could not reach Cloudflare: {str(e)[:160]}")
        content_type = (getattr(r, "headers", {}) or {}).get("content-type", "")
        if r.status_code == 200 and content_type.startswith("image/"):          # some models return the raw picture
            return r.content, sniff(r.content)[0], "", model
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        if r.status_code == 400:
            # "Additional or unevaluated properties '/steps' not allowed": drop that setting and try again
            bad = re.search(r"properties '/(\w+)'", cloudflare_text(payload))
            if bad and bad.group(1) in body and bad.group(1) != "prompt":
                body.pop(bad.group(1))
                continue
        if r.status_code != 200 or payload.get("success") is False:
            raise ImageError(cloudflare_message(r.status_code, payload))
        result = payload.get("result", payload)
        b64 = result.get("image") if isinstance(result, dict) else None
        if not b64:
            raise ImageError("Cloudflare answered, but no picture came back. Try rewording the description.")
        data = base64.b64decode(b64)
        return data, sniff(data)[0], "", model
    raise ImageError("Cloudflare kept rejecting the request settings. Check cloudflare_model in ai_workspace_data/images.json.")


# ----------------------------------------------------------------------
# Google Gemini image models ("Nano Banana") - optional
# ----------------------------------------------------------------------
def gemini_message(status, payload):
    message = ""
    try:
        message = (payload.get("error") or {}).get("message", "")
    except AttributeError:
        pass
    low = message.lower()
    if status in (400, 403, 429) and any(w in low for w in ("billing", "free tier", "not available", "quota", "paid", "permission")):
        return ("Google refused the request: image models are not part of the free Gemini API tier. "
                f"Switch on billing for the project behind your key, or use  /image provider cloudflare.  Google said: {message[:200]}")
    if status == 429:
        return f"Too many requests or the quota is used up. {message[:160]}"
    if status in (401, 403):
        return f"Google did not accept the API key. {message[:160]}"
    return f"Google returned error {status}. {message[:240]}"


def gemini_image(api_key, models, prompt, post=httpx.post, timeout=120):
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
    last = "No image model is configured."
    for model in models:
        try:
            r = post(GEMINI_URL.format(model=model), json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            raise ImageError(f"Could not reach Google: {str(e)[:160]}")
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        if r.status_code == 404:
            last = f"Model '{model}' was not found for your key."
            continue
        if r.status_code != 200:
            raise ImageError(gemini_message(r.status_code, payload))
        candidates = payload.get("candidates") or []
        parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
        note = " ".join(p["text"] for p in parts if p.get("text")).strip()
        for p in parts:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                data = base64.b64decode(inline["data"])
                return data, sniff(data)[0], note, model
        reason = (payload.get("promptFeedback") or {}).get("blockReason") or (candidates[0].get("finishReason") if candidates else "")
        raise ImageError("The model returned no picture" + (f" ({reason})" if reason else "") + ". Try rewording the description.")
    raise ImageError(last + " Use  /image models  to see the image models your key can use.")


def gemini_models(api_key, get=httpx.get):
    r = get(GEMINI_LIST, headers={"x-goog-api-key": api_key}, timeout=30)
    if r.status_code != 200:
        raise ImageError(gemini_message(r.status_code, r.json() if r.content else {}))
    names = [m.get("name", "").replace("models/", "") for m in r.json().get("models", [])]
    return sorted(n for n in names if "image" in n or "banana" in n)


# ----------------------------------------------------------------------
class Usage:
    def __init__(self, path):
        self.path = Path(path)

    def count(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data["count"] if data.get("date") == str(date.today()) else 0
        except (OSError, ValueError, KeyError):
            return 0

    def add(self):
        self.path.write_text(json.dumps({"date": str(date.today()), "count": self.count() + 1}), encoding="utf-8")


def make_picture(cfg, env, prompt):
    """Call the chosen provider. Returns (bytes, mime, note, model). `env` is a dict of environment variables."""
    if cfg["provider"] == "gemini":
        key = env.get("GEMINI_API_KEY")
        if not key:
            raise ImageError("GEMINI_API_KEY is missing in your key.env file.")
        return gemini_image(key, cfg["gemini_models"], prompt)
    account, token = env.get("CLOUDFLARE_ACCOUNT_ID"), env.get("CLOUDFLARE_API_TOKEN")
    if not (account and token):
        raise ImageError("Cloudflare is not set up yet. Add CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN to key.env "
                         "and restart Cerry (type /image help for the steps).")
    return cloudflare_image(account.strip(), token.strip(), cfg["cloudflare_model"], prompt, cfg["steps"])


def install(ns):
    App = ns["App"]
    if App.__dict__.get("_im_installed"):
        return
    App._im_installed = True
    data_dir = ns["DATA_DIR"]
    cfg_path = data_dir / "images.json"
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if cfg["provider"] not in ("cloudflare", "gemini"):
        cfg["provider"] = "cloudflare"

    def save_cfg():
        try:
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    save_cfg()
    usage = Usage(data_dir / "images_usage.json")
    events = queue.Queue()
    busy = {"on": False}

    def viewer(app, path, prompt, model):
        win = tk.Toplevel(app)
        win.title("Cerry image")
        try:
            from PIL import Image, ImageTk
            im = Image.open(path)
            im.thumbnail((900, 760))
            photo = ImageTk.PhotoImage(im)
            lab = ttk.Label(win, image=photo)
            lab.image = photo
            lab.pack(padx=8, pady=8)
        except Exception:
            ttk.Label(win, text="(install pillow to preview pictures here)").pack(padx=20, pady=20)
        ttk.Label(win, text=f"{prompt[:160]}\n{model}  -  {path}", wraplength=860, style="Muted.TLabel").pack(padx=8)

        def save_copy():
            target = filedialog.asksaveasfilename(parent=win, defaultextension=Path(path).suffix, initialfile=Path(path).name)
            if target:
                Path(target).write_bytes(Path(path).read_bytes())

        row = ttk.Frame(win, padding=8)
        row.pack(fill="x")
        ttk.Button(row, text="Save a copy...", command=save_copy).pack(side="left", padx=3)
        ttk.Button(row, text="Open folder", command=lambda: ns["open_folder"](Path(path).parent)).pack(side="left", padx=3)
        ttk.Button(row, text="Close", command=win.destroy).pack(side="right", padx=3)

    def start(app, prompt):
        if busy["on"]:
            app.show("A picture is still being made - please wait for it.")
            return
        if app.policy.local_only:
            app.show("Local-only mode is on, so pictures cannot be made (the image services are online).")
            return
        limit = int(cfg.get("daily_limit", 40))
        if usage.count() >= limit:
            app.show(f"Daily picture limit reached ({limit}). Change daily_limit in ai_workspace_data/images.json.")
            return
        folder = app.ws.dir / "images"
        busy["on"] = True
        app.status.set("Making the picture... (about 5-30 seconds)")
        env = dict(os.environ)

        def job():
            try:
                data, mime, note, model = make_picture(cfg, env, prompt)
                folder.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r"[^\w\-]+", "_", prompt)[:30].strip("_") or "image"
                path = folder / f"gen_{time.strftime('%Y%m%d_%H%M%S')}_{slug}{sniff(data)[1]}"
                path.write_bytes(data)
                usage.add()
                events.put(("done", str(path), prompt, model, note))
            except ImageError as e:
                events.put(("error", str(e)))
            except Exception as e:
                events.put(("error", f"Unexpected problem: {str(e)[:200]}"))
        threading.Thread(target=job, daemon=True).start()

    def list_models(app):
        key = os.getenv("GEMINI_API_KEY")
        if cfg["provider"] != "gemini":
            app.show("/image models only applies to the gemini provider. Cloudflare uses: " + cfg["cloudflare_model"])
            return
        if not key:
            app.show("GEMINI_API_KEY is missing in your key.env file.")
            return

        def job():
            try:
                names = gemini_models(key)
                events.put(("note", "Image models for your key:\n" + ("\n".join(names) or "(none found)")))
            except Exception as e:
                events.put(("error", str(e)[:300]))
        threading.Thread(target=job, daemon=True).start()

    def command(app, text):
        rest = text[len("/image"):].strip()
        low = rest.lower()
        if low in ("", "help"):
            app.show(HELP + f"\n\nCurrent provider: {cfg['provider']}")
        elif low.startswith("provider"):
            name = low.split(None, 1)[1].strip() if " " in low else ""
            if name in ("cloudflare", "gemini"):
                cfg["provider"] = name
                save_cfg()
                app.show(f"Picture provider set to {name}.")
            else:
                app.show(f"Current provider: {cfg['provider']}.  Use  /image provider cloudflare  or  /image provider gemini")
        elif low == "models":
            list_models(app)
        else:
            start(app, rest)

    def drain(app):
        try:
            while True:
                ev = events.get_nowait()
                if ev[0] == "done":
                    _, path, prompt, model, note = ev
                    busy["on"] = False
                    left = int(cfg.get("daily_limit", 40)) - usage.count()
                    app.show(f"🖼 Picture saved: {path}\nModel: {model}   ({left} left today by your own limit)" + (f"\n{note}" if note else ""))
                    app.status.set("Picture ready.")
                    viewer(app, path, prompt, model)
                elif ev[0] == "error":
                    busy["on"] = False
                    app.show("⚠ " + ev[1])
                    app.status.set("Picture failed.")
                elif ev[0] == "note":
                    app.show(ev[1])
        except queue.Empty:
            pass
        app.after(300, lambda: drain(app))

    orig_init, orig_send = App.__init__, App.send

    def init(app, *a, **k):
        orig_init(app, *a, **k)
        strip = ttk.Frame(app, padding=(6, 0))
        strip.pack(fill="x", before=app.inp.master)

        def ask():
            prompt = simpledialog.askstring("Make a picture", "What should Cerry draw?", parent=app)
            if prompt and prompt.strip():
                start(app, prompt.strip())
        ttk.Button(strip, text="🎨 Image...", command=ask).pack(side="left", padx=2)
        ttk.Label(strip, text="or type /image <description>", style="Muted.TLabel").pack(side="left", padx=8)
        app.after(300, lambda: drain(app))

    def send(app):
        text = app.inp.get("1.0", "end").strip()
        if text.lower().startswith("/image"):
            app.inp.delete("1.0", "end")
            command(app, text)
            return
        orig_send(app)

    App.__init__, App.send = init, send
    App._im_api = {"command": command, "events": events, "cfg": cfg, "usage": usage}
