"""Cerry vision: paste / attach images, take screenshots, transcribe audio files.

Loaded automatically by cerri_extensions.install(). Needs: pip install pillow
Settings live in  ai_workspace_data/vision.json  (created on first start).
"""
import base64
import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from PIL import Image, ImageGrab, ImageOps       # ImportError here is reported by cerri_extensions

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_TYPES = "*.wav *.mp3 *.m4a *.ogg *.flac *.webm *.mp4 *.mpeg *.mpga"
MAX_SIDE = 1568            # larger images are scaled down before sending
MAX_IMAGES = 4             # per message
VISION_HINTS = ("gemini", "-vl", "vl-", "vision", "llava", "pixtral", "llama-4", "gpt-4o", "gpt-4.1", "gemma-3")
SCREEN_RE = re.compile(r"\b(my screen|the screen|this screen|on screen|screenshot|bildschirm)\b", re.I)
DEFAULT_CONFIG = {
    "vision_ids": [],          # extra model ids that can read images, e.g. ["qwen-vl"]
    "extra_models": [],        # e.g. [{"label": "Local Qwen3-VL 4B", "provider": "anythingllm", "id": "qwen-vl"}]
    "history_images": 1,       # how many earlier image messages are re-sent for follow-up questions
    "voice_screen": True,      # saying "look at my screen" in conversation mode captures the screen
}
IMAGE_HINT = ("The user attached one or more images. Look at them carefully, describe what is visible, "
              "and say so when something is unclear. Do not claim to see anything that is not in the image.")


# ----------------------------------------------------------------------
# helpers that do not need the window
# ----------------------------------------------------------------------
def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        try:
            Path(path).write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        except OSError:
            pass
    return cfg


def save_image(folder, img, stem="img"):
    """Scale, flatten transparency and store an image. Returns the saved Path."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, "white")
        flat.paste(rgba, mask=rgba.split()[-1])
        img = flat
    else:
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", stem)[:30] or "img"
    name = f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    path = folder / (name + ".png")
    img.save(path, "PNG", optimize=True)
    if path.stat().st_size > 1_500_000:          # big photos: JPEG is much smaller
        path.unlink()
        path = folder / (name + ".jpg")
        img.save(path, "JPEG", quality=88)
    return path


def data_url(path):
    path = Path(path)
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def with_images(message, urls):
    """Turn a text message into an OpenAI-style message with image parts."""
    content = message["content"]
    parts = [{"type": "text", "text": content if isinstance(content, str) else str(content)}]
    parts += [{"type": "image_url", "image_url": {"url": u}} for u in urls]
    out = dict(message)
    out["content"] = parts
    return out


def clipboard_items():
    """Images (PIL) or image file paths currently on the clipboard."""
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        return []
    if data is None:
        return []
    if isinstance(data, list):
        return [Path(p) for p in data if Path(p).suffix.lower() in IMAGE_EXT]
    return [data]


# ----------------------------------------------------------------------
# installation
# ----------------------------------------------------------------------
def install(ns):
    App = ns["App"]
    if getattr(App, "_cv_installed", False):
        return
    App._cv_installed = True
    streaming, routing = ns["streaming"], ns["routing"]
    cfg = load_config(ns["DATA_DIR"] / "vision.json")
    holder = {}

    models = ns["MODELS"]
    for extra in cfg.get("extra_models", []):
        if isinstance(extra, dict) and extra.get("id") and not any(m["label"] == extra.get("label") for m in models):
            models.append({"label": extra.get("label", extra["id"]), "provider": extra.get("provider", "anythingllm"),
                           "id": extra["id"], "vision": True})

    def is_vision(m):
        if "vision" in m:
            return bool(m["vision"])
        mid = m["id"].lower()
        return m["id"] in cfg["vision_ids"] or any(h in mid for h in VISION_HINTS)

    def offline(app):
        ext = getattr(app, "ext_offline", None)
        return bool(app.policy.local_only or (ext is not None and ext.get()))

    def usable_vision_models(app):
        found = [m for m in models if is_vision(m)]
        if offline(app):
            found = [m for m in found if routing.is_local(m)]
        return found

    def load_urls(app, names):
        folder = app.ws.dir / "images"
        urls = []
        for n in names or []:
            p = folder / n
            if p.exists():
                urls.append(data_url(p))
        return urls

    # ---------------- window parts ----------------
    def refresh_chips(app):
        from PIL import ImageTk                                   # needs the Tk runtime
        for w in app._cv_chips.winfo_children():
            w.destroy()
        app._cv_thumbs = []
        for item in app._cv_pending:
            frame = ttk.Frame(app._cv_chips)
            frame.pack(side="left", padx=3)
            try:
                im = Image.open(item["path"])
                im.thumbnail((48, 48))
                photo = ImageTk.PhotoImage(im)
                app._cv_thumbs.append(photo)
                ttk.Label(frame, image=photo).pack(side="left")
            except Exception:
                ttk.Label(frame, text="🖼").pack(side="left")
            ttk.Button(frame, text="✕", width=2, command=lambda it=item: remove_pending(app, it)).pack(side="left")

    def remove_pending(app, item):
        if item in app._cv_pending:
            app._cv_pending.remove(item)
            try:
                Path(item["path"]).unlink()
            except OSError:
                pass
        refresh_chips(app)

    def add_item(app, item, stem="pasted"):
        if len(app._cv_pending) >= MAX_IMAGES:
            app.status.set(f"At most {MAX_IMAGES} images per message.")
            return False
        try:
            if isinstance(item, Path):
                stem = item.stem
                img = Image.open(item)
                img.load()
            else:
                img = item
            path = save_image(app.ws.dir / "images", img, stem)
        except Exception as e:
            app.status.set(f"Could not read the image: {str(e)[:120]}")
            return False
        app._cv_pending.append({"name": path.name, "path": str(path)})
        refresh_chips(app)
        app.status.set(f"Image added ({len(app._cv_pending)}). Ask your question and press Send.")
        return True

    def paste(app, event=None):
        items = clipboard_items()
        added = False
        for it in items:
            added = add_item(app, it) or added
        return "break" if added else None          # None: normal text paste continues

    def pick_files(app):
        paths = filedialog.askopenfilenames(
            title="Choose images", filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"), ("All files", "*.*")])
        for p in paths:
            add_item(app, Path(p))

    def grab_screen(app, then=None):
        """Hide Cerry for a moment, capture the whole screen, bring Cerry back."""
        app.withdraw()
        app.update()
        time.sleep(0.3)
        try:
            shot = ImageGrab.grab()
        except Exception as e:
            app.deiconify()
            app.status.set(f"Screenshot failed: {str(e)[:120]}")
            return False
        app.deiconify()
        ok = add_item(app, shot, "screen")
        if ok and then:
            then()
        return ok

    def audio_file(app):
        path = filedialog.askopenfilename(title="Choose an audio file",
                                          filetypes=[("Audio", AUDIO_TYPES), ("All files", "*.*")])
        if not path:
            return
        app.status.set("Transcribing audio file...")
        voice = ns["voice"]

        def job():
            try:
                text = voice.transcribe(path, app.voice_cfg, ns["get_client"], app.policy.local_only,
                                        on_status=lambda t: app.q.put(("status", t)))
                if text:
                    app.q.put(("transcript", f"[Transcript of {Path(path).name}]\n{text}\n"))
                else:
                    app.q.put(("status", "No speech found in that file."))
            except Exception as e:
                app.q.put(("status", f"Audio transcription failed: {str(e)[:200]}"))
        threading.Thread(target=job, daemon=True).start()

    # ---------------- wrapped App methods ----------------
    orig_init = App.__init__

    def init(app, *a, **k):
        app._cv_pending, app._cv_current, app._cv_attach = [], [], {}
        app._cv_new_turn = app._cv_edit_restored = False
        app._cv_thumbs = []
        holder["app"] = app
        orig_init(app, *a, **k)
        strip = ttk.Frame(app, padding=(6, 0))
        strip.pack(fill="x", before=app.inp.master)
        ttk.Button(strip, text="🖼 Image...", command=lambda: pick_files(app)).pack(side="left", padx=2)
        ttk.Button(strip, text="📋 Paste image", command=lambda: paste(app)).pack(side="left", padx=2)
        ttk.Button(strip, text="🖥 Screen", command=lambda: grab_screen(app)).pack(side="left", padx=2)
        ttk.Button(strip, text="🎧 Audio file...", command=lambda: audio_file(app)).pack(side="left", padx=2)
        ttk.Label(strip, text="Ctrl+V pastes a copied image", style="Muted.TLabel").pack(side="left", padx=8)
        app._cv_chips = ttk.Frame(strip)
        app._cv_chips.pack(side="left", padx=6)
        app.inp.bind("<Control-v>", lambda e: paste(app, e))
        app.inp.bind("<Control-V>", lambda e: paste(app, e))

    orig_send = App.send

    def send(app):
        if not app.busy and app._cv_pending:
            candidates = usable_vision_models(app)
            if not candidates:
                why = " (offline / local-only mode is on)" if offline(app) else ""
                app.show("No model that can read images is available" + why + ". "
                         "Select a Gemini or Nemotron VL model, or add a local vision model in vision.json.")
                return
            chosen = app.selected_model()
            if not is_vision(chosen) or chosen not in candidates:
                app.status.set(f"{chosen['label']} cannot read images - using {candidates[0]['label']}.")
            if not app.inp.get("1.0", "end").strip():
                app.inp.insert("1.0", "Describe this image and point out anything notable.")
        orig_send(app)

    orig_submit = App.submit

    def submit(app, text, base):
        app._cv_new_turn = True
        try:
            orig_submit(app, text, base)
        finally:
            app._cv_new_turn = False
            app._cv_edit_restored = False

    orig_start = App.start_generation

    def start_generation(app, text, base, target):
        msgs = app.chat["messages"]
        idx = len(base)
        user = msgs[idx] if idx < len(msgs) and msgs[idx]["role"] == "user" else None
        names = []
        if user is not None:
            names = user.get("images") or []
            if app._cv_new_turn and app._cv_pending and not names:
                names = [p["name"] for p in app._cv_pending]
                user["images"] = names
                app.ws.save_chat(app.chat)
                app._cv_pending = []
                refresh_chips(app)
        app._cv_current = names
        orig_start(app, text, base, target)

    orig_build = App.build_messages

    def build_messages(app, user_text, base):
        msgs, ev = orig_build(app, user_text, base)
        attach = {}
        keep = int(cfg.get("history_images", 1))
        if keep > 0:
            hist = base[-20:]
            found = [j for j, m in enumerate(hist) if m["role"] == "user" and m.get("images")]
            for j in found[-keep:]:
                urls = load_urls(app, hist[j]["images"])
                if urls:
                    attach[1 + j] = urls
        urls = load_urls(app, app._cv_current)
        if urls:
            attach[len(msgs) - 1] = urls
        app._cv_attach = attach
        return msgs, ev

    orig_render = App.render_chat

    def render_chat(app, *a, **k):
        orig_render(app, *a, **k)
        w = app.chat_w
        w.config(state="normal")
        for i, m in enumerate(app.chat["messages"]):
            if m["role"] == "user" and m.get("images"):
                r = w.tag_ranges(f"edit{i}")
                if r:
                    w.insert(r[1], "   🖼 " + ", ".join(m["images"]), "sys")
        w.config(state="disabled")

    orig_edit = App.start_edit

    def start_edit(app, index):
        orig_edit(app, index)
        if app.editing != index:
            return
        names = app.chat["messages"][index].get("images") or []
        folder = app.ws.dir / "images"
        restored = [{"name": n, "path": str(folder / n)} for n in names if (folder / n).exists()]
        if restored:
            app._cv_pending = restored
            app._cv_edit_restored = True
            refresh_chips(app)

    orig_cancel = App.cancel_edit

    def cancel_edit(app, *a, **k):
        if app._cv_edit_restored:
            app._cv_pending = []
            app._cv_edit_restored = False
            refresh_chips(app)
        return orig_cancel(app, *a, **k)

    App.__init__, App.send, App.submit = init, send, submit
    App.start_generation, App.build_messages = start_generation, build_messages
    App.render_chat, App.start_edit, App.cancel_edit = render_chat, start_edit, cancel_edit

    if hasattr(App, "handle_convo_text"):
        orig_convo = App.handle_convo_text

        def handle_convo_text(app, text):
            if cfg.get("voice_screen", True) and SCREEN_RE.search(text) and usable_vision_models(app):
                grab_screen(app)
            orig_convo(app, text)
        App.handle_convo_text = handle_convo_text

    # ---------------- choose an image-capable model ----------------
    orig_run = streaming.run_chat

    def run_chat(order, messages, get_client, token, policy, on_status=None, on_delta=None):
        app = holder.get("app")
        attach = getattr(app, "_cv_attach", None) if app else None
        if attach and messages and messages[0].get("content") != ns.get("DECIDE_PROMPT"):
            messages = copy.deepcopy(messages)
            for idx, urls in attach.items():
                if idx < len(messages) and messages[idx]["role"] == "user" and isinstance(messages[idx]["content"], str):
                    messages[idx] = with_images(messages[idx], urls)
            messages[0]["content"] += "\n\n" + IMAGE_HINT
        if any(isinstance(m.get("content"), list) for m in messages):
            usable = [m for m in order if is_vision(m)]
            if not usable:
                raise streaming.AllFailed("No model that can read images is available for this request.")
            if on_status and usable[0] is not order[0]:
                on_status(f"{order[0]['label']} cannot read images - trying {usable[0]['label']}.")
            order = usable
        return orig_run(order, messages, get_client, token, policy, on_status, on_delta)

    streaming.run_chat = run_chat
