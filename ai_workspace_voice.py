"""Cherry (Cerry) - AI Workspace.   RUN THIS FILE:  python ai_workspace_voice.py   (start_cerry.bat does that)

Layout of the program
  ai_workspace_voice.py   this file: the window (App), models/providers, prompts, chat flow
  core/                   reusable engines, workspace logic, and the plugin loader
  plugins/cerry_*.py      plugins (models, audio, vision, images, export, agents, jobs, ...)
  config/                 safe example settings copied into ai_workspace_data/ on first setup
Keys live in key.env next to this file.  Everything the program saves is in ai_workspace_data/.
"""
import copy, os, re, sys, json, uuid, shutil, subprocess, threading, queue, datetime, logging
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox, scrolledtext

from dotenv import load_dotenv

try:
    APP_DIR = Path(__file__).resolve().parent
except NameError:
    APP_DIR = Path.cwd()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
CORE_DIR = APP_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import routing, streaming, evidence, voice, realtime_voice
import cerri_extensions
from workspace_sidebar import WorkspaceSidebar
from temporary_chat import TemporaryChat
from turn_context import TurnContext
import backup_tools
from doc_manager import DocumentIndex, DocumentPanel, read_full_text
import chat_format
import workspace
from workspace import Workspace, list_workspaces, write_atomic

load_dotenv(APP_DIR / "key.env")
load_dotenv("key.env")
load_dotenv(APP_DIR / ".env")
load_dotenv()

DATA_DIR = APP_DIR / "ai_workspace_data"
WS_DIR = DATA_DIR / "workspaces"
GLOBAL_MEMORY = DATA_DIR / "global_memory.md"
ROUTING_PATH = DATA_DIR / "routing.json"
VOICE_PATH = DATA_DIR / "voice.json"
VOICES_DIR = DATA_DIR / "voices"
VOICE_TMP = DATA_DIR / "recording_tmp.wav"

APP_NAME = "Cherry"
SETTINGS_PATH = DATA_DIR / "settings.json"
workspace.configure(WS_DIR)

THEMES = {
    "light": {
        "bg": "#f7f7f8", "panel": "#ffffff", "chat_bg": "#ffffff", "fg": "#202123", "muted": "#777a80",
        "input_bg": "#ffffff", "user_bubble": "#eaecef", "code_bg": "#f5f6f8", "accent": "#10a37f",
        "btn": "#f1f2f4", "btn_active": "#e7e8eb", "border": "#dedfe3",
        "select": "#d9e7ff", "user": "#202123", "sys": "#777a80", "link": "#2563eb",
        "think": "#666b73", "refs": "#28734a",
    },
    "dark": {
        "bg": "#17181b", "panel": "#202124", "chat_bg": "#202124", "fg": "#e7e8ea", "muted": "#a0a3aa",
        "input_bg": "#25272b", "user_bubble": "#34363a", "code_bg": "#292b30", "accent": "#10a37f",
        "btn": "#2b2d32", "btn_active": "#373a40", "border": "#3a3c42",
        "select": "#3b4e69", "user": "#ffffff", "sys": "#a0a3aa", "link": "#8ab4f8",
        "think": "#b0b3b9", "refs": "#8fd0a8",
    },
}


def load_settings():
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).exception("Could not read settings file %s", SETTINGS_PATH)
        return {}


def save_settings(data):
    write_atomic(SETTINGS_PATH, json.dumps(data, indent=2))


_VERSION_KEYS = ("content", "model", "reasoning", "sources", "query", "evidence", "stopped")


def _snapshot_version(message):
    return {key: copy.deepcopy(message[key]) for key in _VERSION_KEYS if key in message}


def _make_assistant(version):
    message = {"role": "assistant"}
    message.update(copy.deepcopy(version))
    return message


def _ensure_versions(message):
    if "versions" not in message:
        message["versions"] = [_snapshot_version(message)]
        message["active"] = 0


def _apply_version(message, version):
    for key in _VERSION_KEYS:
        if key in version:
            message[key] = copy.deepcopy(version[key])
        else:
            message.pop(key, None)


def _add_version(message, version):
    _ensure_versions(message)
    message["versions"].append(copy.deepcopy(version))
    _set_active_version(message, len(message["versions"]) - 1)


def _set_active_version(message, index):
    _ensure_versions(message)
    index = max(0, min(index, len(message["versions"]) - 1))
    _apply_version(message, message["versions"][index])
    message["active"] = index


def _version_info(message):
    return message.get("active", 0) + 1, len(message.get("versions", [None]))


def _has_versions(message):
    return len(message.get("versions", [])) > 1


def _branch_chat(chat, upto_index, new_id):
    title = chat.get("title", "Chat")
    if not title.startswith("Branch: "):
        title = "Branch: " + title
    return {"id": new_id, "title": title[:60], "parent": chat.get("id"),
            "messages": copy.deepcopy(chat["messages"][:upto_index + 1])}
for p in (DATA_DIR, WS_DIR):
    p.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=DATA_DIR / "cherry.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s", encoding="utf-8")
GLOBAL_MEMORY.touch(exist_ok=True)

# ----------------------------------------------------------------------
# PROVIDERS AND MODELS  (edit the ids here if a model name changes)
# ----------------------------------------------------------------------
PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("http://127.0.0.1:11434/v1", ""),
}

MODELS = [
    {"label": "Gemini 3.5 Flash-Lite",     "provider": "gemini",     "id": "gemini-3.5-flash-lite"},
    {"label": "Gemini 3.8 Flash",          "provider": "gemini",     "id": "gemini-3.8-flash"},
    {"label": "Gemini 3.7 Flash",          "provider": "gemini",     "id": "gemini-3.7-flash"},
    {"label": "Gemini 3.6 Flash",          "provider": "gemini",     "id": "gemini-3.6-flash"},
    {"label": "Gemini 3.5 Flash",          "provider": "gemini",     "id": "gemini-3.5-flash"},
    {"label": "Groq Qwen 3.8-27b",         "provider": "groq",       "id": "qwen/qwen3.8-27b"},
    {"label": "OpenRouter Nemotron 3.5 Super VL", "provider": "openrouter", "id": "nvidia/nemotron-3.5-super-vl:free"},
    {"label": "Local Gemma 4 (Ollama)", "provider": "ollama", "id": "gemma4:latest"},
    {"label": "Groq GPT-OSS 120B",         "provider": "groq",       "id": "openai/gpt-oss-120b"},

]

BASE_SYSTEM = (
    f"Your name is {APP_NAME}. You are a helpful, precise assistant inside a desktop chat app. "
    f"If asked which language model runs you, say it depends on the model selected in the app "
    f"and do not guess. "
    "When document excerpts are provided, use them when relevant, cite them as [D1], [D2], ... and name the file. "
    r"In written answers use \(…\) for inline mathematics and \[…\] for display equations. "
    "Put reusable code, commands, or a requested copy-ready text in fenced Markdown blocks. "
    "Reply in English by default. If the user writes or speaks German, or asks for German, reply in German. "
)

VOICE_STYLE = (
    "This is a spoken conversation. Answer in plain, natural spoken sentences, usually one to four. "
    "Do not use markdown, bullet lists, tables, code blocks, URLs or citation markers such as [D1]. "
    "If a long or technical answer is needed, give a short spoken summary and offer to write out the details."
)

CONVO_EXIT = {"stop", "stopp", "stop conversation", "end conversation", "stop talking", "goodbye", "bye",
              "beenden", "gespräch beenden", "unterhaltung beenden", "tschüss", "auf wiedersehen"}

DECIDE_PROMPT = (
    "Decide if answering the user's message needs up-to-date information from the web "
    "(news, prices, job postings, software versions, recent papers, anything after your "
    "training data). Reply with exactly NO if not. Otherwise reply with exactly "
    "'SEARCH: <short web search query>'."
)

_clients = {}
_clients_lock = threading.Lock()


def get_client(provider):
    """One shared client per provider.  The OpenAI package is imported on first use (faster start-up)."""
    client = _clients.get(provider)
    if client is not None:
        return client
    with _clients_lock:
        client = _clients.get(provider)
        if client is None:
            if provider not in PROVIDERS:
                raise routing.ConfigError(f"Unknown provider '{provider}'. Add it under Manage models > Providers.")
            base_url, env = PROVIDERS[provider]
            key = (os.getenv(env) or "").strip() if env else ""
            if provider == "ollama" and not env:
                key = "ollama"  # The SDK requires a value. Local Ollama ignores it.
            if not key:
                raise routing.ConfigError(f"{env} is missing in your key.env file")
            from openai import OpenAI
            client = _clients[provider] = OpenAI(api_key=key, base_url=base_url,
                                                 timeout=600 if provider == "ollama" else 120)
    return client


def open_folder(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
    except Exception as e:
        messagebox.showerror("Cannot open folder", str(e))


def web_search(query, n=5):
    """Return a list of dicts: title, url, content. Tavily if key exists, else DuckDuckGo."""
    key = os.getenv("TAVILY_API_KEY")
    if key:
        from tavily import TavilyClient
        res = TavilyClient(api_key=key).search(query, max_results=n)
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "content": (r.get("content") or "")[:1200]} for r in res.get("results", [])]
    from ddgs import DDGS
    res = DDGS().text(query, max_results=n)
    return [{"title": r.get("title", ""), "url": r.get("href", ""),
             "content": (r.get("body") or "")[:1200]} for r in res]


# ----------------------------------------------------------------------
# APP
# ----------------------------------------------------------------------
class App(tk.Tk):
    defer_sidebar_layout = False

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} - AI Workspace")
        self.geometry("1280x860")
        self.minsize(900, 620)
        self.q = queue.Queue()
        self.settings = load_settings()
        self.theme_name = self.settings.get("theme", "light")
        self.policy = routing.RoutingPolicy.load(ROUTING_PATH)
        self.voice_cfg = voice.VoiceSettings.load(VOICE_PATH)
        self.speaker = voice.Speaker(self.voice_cfg, VOICES_DIR,
                                     on_status=lambda t: self.q.put(("status", t)))
        self.recorder = voice.Recorder()
        self.convo = None
        self.convo_active = False
        self.quick_voice_active = False
        self._quick_voice_restore = None
        self.realtime_voice = None
        self._rt_user_text = ""
        self._rt_assistant_text = ""
        self._rt_last_error = ""
        self.convo_stream_id = None
        self.convo_pending = None
        self.typed_followup = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        names = list_workspaces()
        last = self.settings.get("last_workspace")
        self.ws = Workspace(last if last in names else ("default" if "default" in names else names[0]))
        self.chat = None
        self.chat_ids = []
        self.editing = None
        self.pending_files = []
        self.busy = False
        self.token = None
        self.live = None
        self.live_dirty = False
        self.doc_panel = None
        self.flipped = set()
        self._links, self._bound = {}, set()
        self.chat_label_var = tk.StringVar(value="Chats")
        # Preserve the retrieval preference saved by versions with the focus slider.
        self.document_focus = max(0, min(100, int(self.settings.get("document_focus", 100))))
        self.build_ui()
        self.apply_theme(self.theme_name)
        self.load_ws(self.ws.name)
        if not self.defer_sidebar_layout:
            self.sidebar.finish_tools()
        self.after(100, self.poll)

    # ---------------- UI ----------------
    def build_ui(self):
        self.sidebar = WorkspaceSidebar(self, self.settings, save_settings)
        controls = self.sidebar.navigation
        ttk.Label(controls, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=12, pady=(10, 2))
        top = ttk.Frame(controls, padding=6)
        self.sidebar.navigation_grid = top
        top.pack(fill="x")

        header = ttk.Frame(self)
        header.pack(fill="x")
        self.header_actions = ttk.Frame(header)
        self.header_actions.pack(side="right", anchor="ne", padx=(0, 10), pady=3)
        self.model_var = tk.StringVar(value=MODELS[0]["label"])
        self.fallback_var = tk.BooleanVar(value=self.policy.allow_fallback)
        self.local_var = tk.BooleanVar(value=self.policy.local_only)
        ttk.Checkbutton(top, text="Auto-fallback", variable=self.fallback_var,
                        command=self.on_policy_toggle).grid(row=0, column=2, padx=6)
        ttk.Checkbutton(top, text="Local only", variable=self.local_var,
                        command=self.on_policy_toggle).grid(row=0, column=3, padx=6)
        ttk.Button(top, text="Routing...", command=self.open_routing).grid(row=0, column=4, padx=2)
        ttk.Button(top, text="List models", command=self.list_models).grid(row=0, column=5, padx=2)
        self.dark_var = tk.BooleanVar(value=(self.theme_name == "dark"))
        appearance = self.sidebar.tool_row("Display")
        ttk.Checkbutton(appearance, text="Dark mode", variable=self.dark_var,
                        command=self.on_theme_toggle).pack()

        ttk.Label(top, text="Workspace", font=("Segoe UI", 12, "bold")).grid(row=1, column=0, sticky="w", pady=3)
        self.ws_var = tk.StringVar()
        self.ws_box = ttk.Combobox(top, textvariable=self.ws_var, state="readonly", width=36, font=("Segoe UI", 13))
        self.ws_box.grid(row=1, column=1, padx=4)
        self.ws_box.bind("<<ComboboxSelected>>", self.on_ws_selected)
        ttk.Button(top, text="New", command=self.new_ws).grid(row=1, column=2, padx=2)
        ttk.Button(top, text="Documents...", command=self.open_doc_panel).grid(row=1, column=3, padx=2)
        ttk.Button(top, text="Open documents folder",
                   command=lambda: open_folder(self.ws.docs)).grid(row=1, column=4, padx=2)
        ttk.Button(top, text="Link extra folder", command=self.link_folder).grid(row=1, column=5, padx=2)
        ttk.Button(top, text="Re-scan", command=self.reindex_async).grid(row=1, column=6, padx=2)

        ttk.Label(top, textvariable=self.chat_label_var, font=("Segoe UI", 12, "bold")).grid(row=2, column=0, sticky="w", pady=3)
        history = ttk.Frame(top)
        history.grid(row=2, column=1, sticky="ew")
        self.chat_titles = tk.StringVar(value=())
        self.chat_box = tk.Listbox(history, listvariable=self.chat_titles, height=4, width=1,
                                   activestyle="none", exportselection=False, borderwidth=0,
                                   highlightthickness=0, font=("Segoe UI", 13))
        history_scroll = ttk.Scrollbar(history, command=self.chat_box.yview)
        history_scroll.pack(side="right", fill="y")
        self.chat_box.configure(yscrollcommand=history_scroll.set)
        self.chat_box.pack(fill="both", expand=True)
        self.chat_box.bind("<<ListboxSelect>>", self.on_chat_selected)
        ttk.Button(top, text="＋ New chat", style="Sidebar.TButton", command=self.new_chat).grid(row=2, column=2, padx=2)
        ttk.Button(top, text="Temporary chat", style="Sidebar.TButton",
                   command=lambda: TemporaryChat(self, MODELS, get_client)).grid(row=2, column=3, padx=2)
        ttk.Button(top, text="Rename", command=self.rename_chat).grid(row=2, column=4, padx=2)
        ttk.Button(top, text="Delete", command=self.delete_chat).grid(row=2, column=5, padx=2)

        mem = self.sidebar.tool_row("Memory")
        self.memory_scope_var = tk.StringVar(value=self.settings.get("memory_scope", "Both"))
        if self.memory_scope_var.get() not in ("None", "Both", "Workspace only"):
            self.memory_scope_var.set("Both")
        self.use_global = tk.BooleanVar()
        self.use_workspace_memory = tk.BooleanVar()
        self.use_ws = tk.BooleanVar(value=True)
        ttk.Label(mem, text="Memory scope").pack(side="left")
        self.memory_scope_box = ttk.Combobox(
            mem, textvariable=self.memory_scope_var, state="readonly", width=16,
            values=["None", "Both", "Workspace only"])
        self.memory_scope_box.pack(side="left", padx=(4, 10))
        self.memory_scope_box.bind("<<ComboboxSelected>>", self.on_memory_scope_change)
        self.on_memory_scope_change()
        workspace_controls = self.sidebar.tool_row("Workspace settings")
        self.workspace_context_check = ttk.Checkbutton(
            workspace_controls, text="Use workspace context", variable=self.use_ws)
        self.workspace_context_check.pack(side="left", padx=(0, 8))
        ttk.Button(mem, text="Edit global memory",
                   command=lambda: self.edit_file("Global memory", GLOBAL_MEMORY)).pack(side="left", padx=4)
        ttk.Button(mem, text="Edit workspace notes",
                   command=lambda: self.edit_file("Workspace notes", self.ws.notes_path)).pack(side="left", padx=4)
        ttk.Button(workspace_controls, text="Edit instructions",
                   command=lambda: self.edit_file("Workspace instructions (style, rules)",
                                                  self.ws.instr_path)).pack(side="left", padx=4)
        ttk.Button(workspace_controls, text="Backup / Restore...", command=self.backup_restore).pack(side="left", padx=4)
        web_controls = self.sidebar.tool_row("Response settings")
        ttk.Label(web_controls, text="Web search").pack(side="left", padx=(12, 2))
        self.web_var = tk.StringVar(value="Auto")
        ttk.Combobox(web_controls, textvariable=self.web_var, state="readonly", width=8,
                     values=["Off", "Auto", "Always"]).pack(side="left")

        doc_source = self.sidebar.tool_row("Workspace settings")
        ttk.Label(doc_source, text="Document source:").pack(side="left")
        self.document_source_var = tk.StringVar(
            value=self.settings.get("document_source_mode", "Workspace documents"))
        if self.document_source_var.get() not in (
                "Workspace documents", "Prioritize chat documents", "Only use chat documents"):
            self.document_source_var.set("Workspace documents")
        self.document_source_box = ttk.Combobox(
            doc_source, textvariable=self.document_source_var, state="readonly", width=28,
            values=["Workspace documents", "Prioritize chat documents", "Only use chat documents"])
        self.document_source_box.pack(side="left", padx=8)
        self.document_source_box.bind("<<ComboboxSelected>>", self.on_document_source_change)
        self.after_idle(self.on_document_source_change)

        view = self.sidebar.tool_row("Display")
        self.show_reason = tk.BooleanVar(value=False)
        self.show_refs = tk.BooleanVar(value=False)
        self.show_evid = tk.BooleanVar(value=False)
        self.branch_edit = tk.BooleanVar(value=False)
        ttk.Checkbutton(view, text="Show reasoning", variable=self.show_reason,
                        command=self.reset_toggles).pack(side="left")
        ttk.Checkbutton(view, text="Show references", variable=self.show_refs,
                        command=self.reset_toggles).pack(side="left", padx=8)
        ttk.Checkbutton(view, text="Show evidence", variable=self.show_evid,
                        command=self.reset_toggles).pack(side="left")
        ttk.Checkbutton(view, text="Branch on message edit",
                        variable=self.branch_edit).pack(side="left", padx=16)

        self.chat_w = scrolledtext.ScrolledText(
            self, wrap="word", state="disabled", font=("Segoe UI", 11),
            padx=22, pady=18, bd=0, relief="flat", highlightthickness=0,
        )
        self.chat_w.pack(fill="both", expand=True, padx=12, pady=(6, 4))
        self._user_bubbles = []
        self._answer_widgets = []
        self.chat_w.tag_config("user", foreground="#202123", font=("Segoe UI", 11), justify="right",
                               spacing1=8, spacing3=8)
        self.chat_w.tag_config("bot", foreground="#202123", font=("Segoe UI", 11), spacing1=3, spacing3=8)
        self.chat_w.tag_config("message_meta", foreground="#777a80", font=("Segoe UI", 9, "bold"),
                               spacing1=16, spacing3=3)
        self.chat_w.tag_config("markdown_heading", font=("Segoe UI", 12, "bold"), spacing1=8, spacing3=4)
        self.chat_w.tag_config("markdown_bold", font=("Segoe UI", 11, "bold"))
        self.chat_w.tag_config("markdown_code", font=("Consolas", 10), background="#f5f6f8")
        self.chat_w.tag_config("markdown_code_block", font=("Consolas", 10), background="#f5f6f8",
                               lmargin1=16, lmargin2=16, rmargin=16, spacing1=4, spacing3=6)
        self.chat_w.tag_config("sys", foreground="#777a80", font=("Segoe UI", 9, "italic"))
        self.chat_w.tag_config("link", foreground="#2563eb", underline=True, font=("Segoe UI", 9))
        self.chat_w.tag_config("think", foreground="#666b73", font=("Segoe UI", 9, "italic"))
        self.chat_w.tag_config("refs", foreground="#28734a", font=("Segoe UI", 9))
        self.chat_w.bind("<Configure>", self.update_chat_layout, add="+")

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status, style="Muted.TLabel").pack(fill="x", padx=20, pady=(0, 2))
        
        bottom = ttk.Frame(self, padding=(16, 6, 16, 14))
        bottom.pack(fill="x")
        composer = ttk.Frame(bottom, padding=(10, 8), style="Composer.TFrame")
        composer.pack(fill="x", expand=True)
        self.inp = tk.Text(composer, height=3, wrap="word", font=("Segoe UI", 11),
                           bd=0, relief="flat", highlightthickness=0, padx=4, pady=4, undo=True)
        self.inp.pack(fill="x", expand=True)
        self.inp.bind("<Return>", self.on_enter)
        self.inp.bind("<Escape>", self.cancel_edit)
        toolbar = ttk.Frame(composer, style="Composer.TFrame")
        toolbar.pack(fill="x", pady=(6, 0))
        toolbar.columnconfigure(0, weight=1)
        btns = ttk.Frame(toolbar, style="Composer.TFrame")
        btns.grid(row=0, column=0, sticky="w")
        actions = ttk.Frame(toolbar, style="Composer.TFrame")
        actions.grid(row=0, column=1, sticky="e")

        def fit_actions(event):
            narrow = event.width < btns.winfo_reqwidth() + actions.winfo_reqwidth() + 8
            if int(actions.grid_info()["row"]) != int(narrow):
                actions.grid(row=int(narrow), column=0 if narrow else 1,
                             columnspan=2 if narrow else 1, sticky="e")

        toolbar.bind("<Configure>", fit_actions)
        self.send_btn = ttk.Button(actions, text="Send", command=self.send_or_interrupt)
        self.send_btn.pack(side="right", padx=(4, 0))
        ttk.Combobox(actions, textvariable=self.model_var, state="readonly", width=23,
                     values=[m["label"] for m in MODELS]).pack(side="right", padx=4)
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right", padx=4)
        ttk.Button(btns, text="＋ Attach", command=self.attach).pack(side="left", padx=(0, 4))
        self.mic_btn = ttk.Button(btns, text="🎤 Record", command=self.toggle_record)
        self.mic_btn.pack(side="left", padx=4)
        self.realtime_voice_btn = ttk.Button(btns, text="🎙 Live", command=self.toggle_realtime_voice)
        self.realtime_voice_btn.pack(side="left", padx=4)
        self.share_screen_btn = ttk.Button(btns, text="Share screen", command=lambda: self.toggle_realtime_voice(share_screen=True))
        self.share_screen_btn.pack(side="left", padx=4)
        voice_tools = self.sidebar.tool_row("Audio and voice")
        self.convo_btn = ttk.Button(voice_tools, text="🗣 Converse", command=self.toggle_conversation)
        self.convo_btn.pack(side="left", padx=4)
        self.quick_voice_btn = ttk.Button(voice_tools, text="⚡ Quick talk", command=self.toggle_quick_conversation)
        self.quick_voice_btn.pack(side="left", padx=4)
        ttk.Button(voice_tools, text="🔇 Silence", command=self.speaker.stop).pack(side="left", padx=4)
        self.auto_read_var = tk.BooleanVar(value=self.voice_cfg.auto_read)
        ttk.Checkbutton(voice_tools, text="Read answers aloud", variable=self.auto_read_var,
                        command=self.on_voice_toggle).pack(side="left", padx=16)
        ttk.Label(voice_tools, text="Microphone language").pack(side="left", padx=(8, 2))
        self.mic_lang_var = tk.StringVar(
            value={"auto": "Auto", "en": "English", "de": "German"}.get(self.voice_cfg.stt_language, "Auto"))
        mic_box = ttk.Combobox(voice_tools, textvariable=self.mic_lang_var, state="readonly", width=8,
                               values=["Auto", "English", "German"])
        mic_box.pack(side="left")
        mic_box.bind("<<ComboboxSelected>>", lambda e: self.on_voice_toggle())
        ttk.Button(voice_tools, text="Voice settings...", command=self.open_voice).pack(side="left", padx=8)

    def on_theme_toggle(self):
        self.theme_name = "dark" if self.dark_var.get() else "light"
        self.settings["theme"] = self.theme_name
        save_settings(self.settings)
        self.apply_theme(self.theme_name)
        if self._answer_widgets:
            self.render_chat(keep_view=True)

    def backup_restore(self):
        choice = messagebox.askyesnocancel(
            "Cherry backup",
            "Yes: create a backup ZIP of chats, documents, settings and plugin data.\n\n"
            "No: restore a Cherry backup into this installation. Existing files are preserved; "
            "a safety copy is made before restoring.\n\nCancel: do nothing.", parent=self)
        if choice is None:
            return
        if choice:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = filedialog.asksaveasfilename(parent=self, title="Save Cherry backup",
                initialfile=f"cherry-backup-{stamp}.zip", defaultextension=".zip",
                filetypes=[("Cherry backup", "*.zip")])
            if not path:
                return
            try:
                backup_tools.create_backup(DATA_DIR, path)
                messagebox.showinfo("Cherry backup", f"Backup saved:\n{path}\n\nAPI keys are not included.", parent=self)
            except Exception as exc:
                messagebox.showerror("Cherry backup", f"Could not create backup: {exc}", parent=self)
            return
        path = filedialog.askopenfilename(parent=self, title="Select Cherry backup",
                                          filetypes=[("Cherry backup", "*.zip")])
        if not path:
            return
        if not messagebox.askyesno("Restore backup", "Restore settings and workspace files from this backup?\n\n"
                                   "Existing files with matching names will be replaced. A safety copy will be made first.",
                                   parent=self):
            return
        try:
            safety = backup_tools.restore_backup(path, DATA_DIR)
            note = f"\nSafety copy: {safety}" if safety else ""
            messagebox.showinfo("Restore complete", "Backup data restored. Restart Cherry to reload settings." + note,
                                parent=self)
        except Exception as exc:
            messagebox.showerror("Restore failed", str(exc), parent=self)

    def on_memory_scope_change(self, _event=None):
        mode = self.memory_scope_var.get()
        self.use_global.set(mode == "Both")
        self.use_workspace_memory.set(mode in ("Both", "Workspace only"))
        if _event is not None:
            self.settings["memory_scope"] = mode
            save_settings(self.settings)

    def on_document_source_change(self, _event=None):
        mode = self.document_source_var.get()
        if _event is not None:
            self.settings["document_source_mode"] = mode
            save_settings(self.settings)
        only_chat = mode == "Only use chat documents"
        self.workspace_context_check.configure(state="disabled" if only_chat else "normal")
        if mode != "Workspace documents" and getattr(self, "_chat_memory", None) is None and hasattr(self, "status"):
            self.status.set("Persistent chat documents need the cerry_chat_memory plugin; message attachments still work for one turn.")
    
    def _walk(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._walk(child)
    
    def apply_theme(self, name):
        t = THEMES[name]
        s = ttk.Style(self)
        s.theme_use("clam")
        self.configure(bg=t["bg"])
        if hasattr(self, "sidebar"):
            self.sidebar.canvas.configure(background=t["bg"])
    
        s.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["input_bg"],
                    bordercolor=t["border"], lightcolor=t["bg"], darkcolor=t["bg"],
                    troughcolor=t["panel"], focuscolor=t["bg"])
        s.configure("TFrame", background=t["bg"])
        s.configure("TLabel", background=t["bg"], foreground=t["fg"])
        s.configure("Muted.TLabel", background=t["bg"], foreground=t["muted"])
        s.configure("TButton", background=t["btn"], foreground=t["fg"], padding=(10, 7), borderwidth=1)
        s.configure("CopyBlock.TButton", padding=(7, 2), font=("Segoe UI", 9))
        s.configure("Accent.TButton", background=t["accent"], foreground="#ffffff",
                    padding=(14, 7), borderwidth=0)
        s.map("Accent.TButton", background=[("active", "#0e906f"), ("disabled", t["btn"])],
              foreground=[("disabled", t["muted"])])
        s.configure("Composer.TFrame", background=t["input_bg"], bordercolor=t["border"],
                    relief="solid", borderwidth=1)
        s.map("TButton",
              background=[("active", t["btn_active"]), ("disabled", t["bg"])],
              foreground=[("disabled", t["muted"])])
        s.configure("TCheckbutton", background=t["bg"], foreground=t["fg"],
                    indicatorbackground=t["input_bg"], indicatorforeground=t["fg"])
        s.map("TCheckbutton", background=[("active", t["bg"])],
              foreground=[("disabled", t["muted"])])
        s.configure("TCombobox", fieldbackground=t["input_bg"], background=t["btn"],
                    foreground=t["fg"], arrowcolor=t["fg"],
                    selectbackground=t["input_bg"], selectforeground=t["fg"])
        s.map("TCombobox",
              fieldbackground=[("readonly", t["input_bg"])],
              foreground=[("readonly", t["fg"])],
              selectbackground=[("readonly", t["input_bg"])],
              selectforeground=[("readonly", t["fg"])])
        s.configure("Treeview", background=t["panel"], fieldbackground=t["panel"],
                    foreground=t["fg"], rowheight=24)
        s.map("Treeview", background=[("selected", t["select"])],
              foreground=[("selected", t["fg"])])
        s.configure("Treeview.Heading", background=t["btn"], foreground=t["fg"])
        s.configure("TScrollbar", background=t["btn"], troughcolor=t["panel"], arrowcolor=t["fg"])
    
        # defaults for windows opened later (editors, previews, dialogs)
        self.option_add("*Toplevel.background", t["bg"])
        self.option_add("*Text.background", t["input_bg"])
        self.option_add("*Text.foreground", t["fg"])
        self.option_add("*Text.insertBackground", t["fg"])
        self.option_add("*Text.selectBackground", t["select"])
    
        # widgets that already exist
        s.configure("Sidebar.TButton", anchor="w", padding=(8, 4), borderwidth=0,
                    background=t["bg"], foreground=t["fg"])
        s.map("Sidebar.TButton", background=[("active", t["btn_active"])])
        s.configure("SidebarSection.TButton", anchor="w", padding=(8, 5), borderwidth=0,
                    font=("Segoe UI", 10, "bold"), background=t["bg"], foreground=t["fg"])
        s.map("SidebarSection.TButton", background=[("active", t["btn_active"])])
        self.chat_box.configure(bg=t["bg"], fg=t["fg"], selectbackground=t["select"],
                                selectforeground=t["fg"])
        self.send_btn.configure(style="Accent.TButton")
        self.chat_w.configure(bg=t["chat_bg"], fg=t["fg"], insertbackground=t["fg"],
                              selectbackground=t["select"], selectforeground=t["fg"])
        self.inp.configure(bg=t["input_bg"], fg=t["fg"], insertbackground=t["fg"],
                          selectbackground=t["select"], selectforeground=t["fg"])
        w = self.chat_w
        w.tag_config("user", foreground=t["user"])
        w.tag_config("user_meta", foreground=t["muted"], font=("Segoe UI", 9, "bold"))
        w.tag_config("message_meta", foreground=t["muted"], font=("Segoe UI", 9, "bold"))
        w.tag_config("markdown_heading", foreground=t["fg"], font=("Segoe UI", 12, "bold"))
        w.tag_config("markdown_bold", foreground=t["fg"], font=("Segoe UI", 11, "bold"))
        w.tag_config("markdown_code", foreground=t["fg"], background=t["code_bg"], font=("Consolas", 10))
        w.tag_config("markdown_code_block", foreground=t["fg"], background=t["code_bg"],
                     font=("Consolas", 10))
        w.tag_config("bot", foreground=t["fg"])
        w.tag_config("sys", foreground=t["sys"])
        w.tag_config("link", foreground=t["link"])
        w.tag_config("think", foreground=t["think"])
        w.tag_config("refs", foreground=t["refs"])
        for bubble in self._user_bubbles:
            bubble.configure(bg=t["user_bubble"], fg=t["user"])
            bubble.master.configure(bg=t["chat_bg"])
        self.update_chat_layout()
    
        # dropdown lists of the comboboxes
        for child in self._walk(self):
            if isinstance(child, ttk.Combobox):
                try:
                    pop = self.tk.call("ttk::combobox::PopdownWindow", str(child))
                    if child is self.ws_box:
                        self.tk.call(f"{pop}.f.l", "configure", "-font", ("Segoe UI", 13))
                    self.tk.call(f"{pop}.f.l", "configure",
                                 "-background", t["input_bg"], "-foreground", t["fg"],
                                 "-selectbackground", t["select"], "-selectforeground", t["fg"])
                except tk.TclError:
                    pass
    
        self.after_idle(lambda: self.set_titlebar_dark(name == "dark"))
    
    def set_titlebar_dark(self, dark):
        """Windows only, best effort. Other systems keep their normal title bar."""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            val = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                    break
        except Exception:
            pass

    def show(self, text, tag="sys"):
        w = self.chat_w
        w.config(state="normal")
        w.insert("end", text + "\n\n", tag)
        w.config(state="disabled")
        w.see("end")

    def add_link(self, text, tag_name, callback):
        """A clickable word in the chat.  Each tag is bound once; later redraws only swap the callback."""
        w = self.chat_w
        w.insert("end", text, ("link", tag_name))
        self._links[tag_name] = callback
        if tag_name not in self._bound:
            self._bound.add(tag_name)
            w.tag_bind(tag_name, "<Button-1>", lambda e, t=tag_name: self._links[t]())
            w.tag_bind(tag_name, "<Enter>", lambda e: w.config(cursor="hand2"))
            w.tag_bind(tag_name, "<Leave>", lambda e: w.config(cursor=""))

    def insert_markdown(self, text, base_tag, streaming=False):
        if streaming:
            self.insert_markdown_text(text, base_tag)
            return
        w = self.chat_w
        theme = THEMES[self.theme_name]
        for kind, content, language in chat_format.spans(text):
            if kind == "text":
                self.insert_markdown_text(content, base_tag)
                continue
            def copy_source(source=content):
                self.clipboard_clear()
                self.clipboard_append(source)
                self.status.set("Copied to clipboard.")

            if kind != "inline":
                if w.get("end-2c", "end-1c") != "\n":
                    w.insert("end", "\n")
                title = "LaTeX" if kind == "display" else (language or "Text")
                bar = tk.Frame(w, bg=theme["code_bg"])
                tk.Label(bar, text=title, bg=theme["code_bg"], fg=theme["muted"],
                         font=("Segoe UI", 9), padx=8).pack(side="left")
                ttk.Button(bar, text="Copy", command=copy_source,
                           style="CopyBlock.TButton").pack(side="right")
                self._answer_widgets.append(bar)
                w.window_create("end", window=bar, padx=4, pady=2)
                w.insert("end", "\n")
            if kind == "code":
                w.insert("end", content, (base_tag, "markdown_code_block"))
                continue
            expr = content.strip()
            for left, right in ((r"\[", r"\]"), (r"\(", r"\)"), ("$$", "$$"), ("$", "$")):
                if expr.startswith(left) and expr.endswith(right):
                    expr = expr[len(left):-len(right)]
                    break
            bitmap = chat_format.equation_bitmap(expr, theme["fg"], round(self.winfo_fpixels("1i")))
            if bitmap is None:
                w.insert("end", content, (base_tag, "markdown_code"))
            else:
                from PIL import ImageTk
                image = ImageTk.PhotoImage(bitmap, master=w)
                label = tk.Label(w, image=image, bg=theme["chat_bg"], bd=0, cursor="hand2")
                label.image = image
                label.bind("<Button-1>", lambda _event, fn=copy_source: fn())
                self._answer_widgets.append(label)
                w.window_create("end", window=label, align="center", padx=2)
            if kind == "display":
                w.insert("end", "\n")

    def insert_markdown_text(self, text, base_tag):
        """Render common Markdown structure in the view while keeping stored message text untouched."""
        code_block = False
        spans = []
        inline = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")
        for line in text.splitlines(keepends=True):
            if line.lstrip().startswith("```"):
                code_block = not code_block
                continue
            if code_block:
                spans.extend((line, (base_tag, "markdown_code_block")))
                continue

            extra = None
            content = line
            heading = re.match(r"^\s{0,3}#{1,3}\s+", content)
            if heading:
                content = content[heading.end():]
                extra = "markdown_heading"
            else:
                bullet = re.match(r"^(\s*)([-*+] |\d+[.)] )", content)
                if bullet:
                    content = content[:bullet.start(2)] + "• " + content[bullet.end(2):]

            for part in inline.split(content):
                if not part:
                    continue
                tags = [base_tag]
                if part.startswith("**") and part.endswith("**"):
                    part = part[2:-2]
                    tags.append("markdown_bold")
                elif part.startswith("`") and part.endswith("`"):
                    part = part[1:-1]
                    tags.append("markdown_code")
                if extra and base_tag != "user":
                    tags.append(extra)
                spans.extend((part, tuple(tags)))
        if spans:
            self.chat_w.insert("end", *spans)

    def update_chat_layout(self, _event=None):
        """Keep answers readable and user messages visually compact as the window resizes."""
        width = max(600, self.chat_w.winfo_width())
        user_left = int(width * 0.34)
        answer_right = int(width * 0.15)
        for bubble in getattr(self, "_user_bubbles", ()):
            bubble.configure(wraplength=max(240, int(width * 0.56)))
        self.chat_w.tag_configure("user", lmargin1=user_left, lmargin2=user_left, rmargin=18,
                                  justify="right", spacing1=8, spacing3=10)
        self.chat_w.tag_configure("user_meta", lmargin1=user_left, lmargin2=user_left, rmargin=18,
                                  justify="right", spacing1=16, spacing3=4)
        self.chat_w.tag_configure("bot", lmargin1=20, lmargin2=20, rmargin=answer_right,
                                  justify="left", spacing1=3, spacing3=10)
        self.chat_w.tag_configure("message_meta", lmargin1=20, lmargin2=20, rmargin=answer_right,
                                  justify="left", spacing1=14, spacing3=2)

    # ---- show/hide reasoning, references, evidence ----
    def reset_toggles(self):
        self.flipped.clear()
        self.render_chat(keep_view=True)

    def is_open(self, i, kind):
        base = {"r": self.show_reason, "s": self.show_refs, "e": self.show_evid}[kind].get()
        return base != ((i, kind) in self.flipped)

    def toggle(self, i, kind):
        self.flipped ^= {(i, kind)}
        self.render_chat(keep_view=True)

    def render_chat(self, keep_view=False):
        w = self.chat_w
        for widget in self._answer_widgets:
            widget.destroy()
        self._answer_widgets.clear()
        for bubble in getattr(self, "_user_bubbles", ()):
            try:
                bubble.master.destroy()
            except tk.TclError:
                pass
        self._user_bubbles = []
        pos = w.yview()
        stick = pos[1] >= 0.98
        w.config(state="normal")
        w.delete("1.0", "end")
        msgs = self.chat["messages"]
        if not msgs and not self.live:
            w.tag_configure("welcome", justify="center", font=("Segoe UI", 19), spacing1=60)
            w.insert("end", "What can I help with?", "welcome")
        for i, m in enumerate(msgs):
            last = (i == len(msgs) - 1)
            if m["role"] == "user":
                theme = THEMES[self.theme_name]
                bubble_row = tk.Frame(w, bg=theme["chat_bg"])
                bubble = tk.Label(
                    bubble_row, text=m["content"], anchor="w", justify="left",
                    wraplength=max(240, int(w.winfo_width() * 0.56)),
                    bg=theme["user_bubble"], fg=theme["user"],
                    font=("Segoe UI", 11), padx=14, pady=10,
                )
                bubble.pack(side="right", anchor="e", padx=(0, 18), pady=(8, 2))
                bubble.update_idletasks()
                bubble_row.configure(width=max(600, w.winfo_width() - 24), height=bubble_row.winfo_reqheight())
                bubble_row.pack_propagate(False)
                self._user_bubbles.append(bubble)
                w.window_create("end", window=bubble_row, stretch=True)
                w.insert("end", "\n")
                self.add_link("   ✎ edit", f"edit{i}", lambda i=i: self.start_edit(i))
                if not self.busy and self.realtime_voice is None:
                    self.add_link("   🗑 delete", f"del{i}", lambda i=i: self.delete_message(i))
                if last and not self.busy:
                    self.add_link("   ↻ retry", "retry", self.regenerate)
                w.insert("end", "\n\n")
                continue
            w.insert("end", f"{APP_NAME} [{m.get('model', '')}]: \n", "message_meta")
            self.insert_markdown(m["content"], "bot")
            if m.get("stopped"):
                w.insert("end", "  [stopped]", "sys")
            w.insert("end", "\n")

            if _has_versions(m):
                cur, total = _version_info(m)
                self.add_link("◀", f"vp{i}", lambda i=i: self.switch_version(i, -1))
                w.insert("end", f" {cur}/{total} ", "sys")
                self.add_link("▶", f"vn{i}", lambda i=i: self.switch_version(i, 1))
                w.insert("end", "    ")
            reasoning = m.get("reasoning", "")
            sources = m.get("sources") or []
            evid = m.get("evidence") or []
            if reasoning:
                self.add_link("▾ Hide reasoning" if self.is_open(i, "r") else "▸ Show reasoning",
                              f"r{i}", lambda i=i: self.toggle(i, "r"))
                w.insert("end", "    ")
            if sources:
                self.add_link(("▾ Hide " if self.is_open(i, "s") else "▸ Show ") + f"References ({len(sources)})",
                              f"s{i}", lambda i=i: self.toggle(i, "s"))
                w.insert("end", "    ")
            if evid:
                self.add_link(("▾ Hide " if self.is_open(i, "e") else "▸ Show ") + f"Evidence ({len(evid)})",
                              f"e{i}", lambda i=i: self.toggle(i, "e"))
                w.insert("end", "    ")
            self.add_link("⑂ branch", f"br{i}", lambda i=i: self.branch_from(i))
            w.insert("end", "    ")
            self.add_link("🔊 speak", f"sp{i}",
                          lambda i=i: self.speaker.speak(self.chat["messages"][i]["content"]))
            if not self.busy and self.realtime_voice is None:
                w.insert("end", "    ")
                self.add_link("🗑 delete", f"del{i}", lambda i=i: self.delete_message(i))
            if last and not self.busy:
                w.insert("end", "    ")
                self.add_link("↻ regenerate", "regen", self.regenerate)
            w.insert("end", "\n")

            if reasoning and self.is_open(i, "r"):
                w.insert("end", "\nReasoning:\n" + reasoning + "\n", "think")
            if sources and self.is_open(i, "s"):
                lines = [f"Web search: {m['query']}"] if m.get("query") else []
                lines += [f"[{k+1}] {s['title']} - {s['url']}" for k, s in enumerate(sources)]
                w.insert("end", "\nReferences:\n" + "\n".join(lines) + "\n", "refs")
            if evid and self.is_open(i, "e"):
                w.insert("end", "\nEvidence from your documents:\n" + evidence.render_lines(evid) + "\n", "refs")
            w.insert("end", "\n")

        if self.live:
            lv = self.live
            w.insert("end", f"{APP_NAME} [{lv['label']}]: \n", "message_meta")
            self.insert_markdown(lv["answer"] + " ▌\n", "bot", streaming=True)
            if lv["reasoning"] and self.show_reason.get():
                w.insert("end", "\nReasoning:\n" + lv["reasoning"] + "\n", "think")
            w.insert("end", "\n")

        w.config(state="disabled")
        if keep_view is True or (keep_view == "auto" and not stick):
            w.yview_moveto(pos[0])
        else:
            w.see("end")

    def delete_message(self, index):
        if self.busy or self.realtime_voice is not None:
            self.status.set("Wait until the current response or voice session ends before deleting messages.")
            return
        messages = self.chat.get("messages", [])
        if not 0 <= index < len(messages):
            return
        message = messages[index]
        role = "your message" if message.get("role") == "user" else f"{APP_NAME}'s reply"
        preview = (message.get("content") or "").replace("\n", " ").strip()
        if len(preview) > 100:
            preview = preview[:97] + "..."
        if not messagebox.askyesno(
                "Delete message",
                f"Delete this {role}?\n\n{preview}\n\nOnly this message will be removed; the rest of the chat will stay.",
                parent=self):
            return
        del messages[index]
        self.flipped = {(i if i < index else i - 1, kind)
                        for i, kind in self.flipped if i != index}
        if self.editing is not None:
            if self.editing == index:
                self.editing = None
                self.inp.delete("1.0", "end")
            elif self.editing > index:
                self.editing -= 1
        if not messages:
            self.chat["title"] = "New chat"
        self.ws.save_chat(self.chat)
        self.refresh_chat_list()
        self.render_chat(keep_view=True)
        self.status.set("Message deleted.")

    # ---------------- voice ----------------
    def on_voice_toggle(self):
        self.voice_cfg.auto_read = self.auto_read_var.get()
        self.voice_cfg.stt_language = {"Auto": "auto", "English": "en", "German": "de"}[self.mic_lang_var.get()]
        self.voice_cfg.save(VOICE_PATH)
    
    def open_voice(self):
        def changed():
            self.auto_read_var.set(self.voice_cfg.auto_read)
            self.mic_lang_var.set(
                {"auto": "Auto", "en": "English", "de": "German"}.get(self.voice_cfg.stt_language, "Auto"))
        voice.open_voice_dialog(self, self.voice_cfg, VOICE_PATH, self.speaker, changed)
    
    def toggle_record(self):
        if self.convo_active:
            self.status.set("Conversation mode is on - end it first to use the record button.")
            return
        if not self.recorder.recording:
            self.speaker.stop()
            try:
                self.recorder.start()
            except Exception as e:
                self.status.set(f"Microphone error: {str(e)[:150]}")
                return
            self.mic_btn.config(text="End & transcribe")
            self.status.set("Recording... click again to stop.")
            return
    
        path = self.recorder.stop(DATA_DIR / f"recording-{uuid.uuid4().hex}.wav")
        self.mic_btn.config(text="🎤 Record")
        if not path:
            self.status.set("Recording too short - nothing to transcribe.")
            return
        self.status.set("Transcribing...")
    
        def job():
            try:
                text = voice.transcribe(path, self.voice_cfg, get_client, self.policy.local_only,
                                        on_status=lambda t: self.q.put(("status", t)), isolated=True)
                self.q.put(("transcript", text))
            except Exception as e:
                self.q.put(("status", f"Transcription failed: {str(e)[:200]}"))
            finally:
                try:
                    Path(path).unlink()
                except OSError:
                    pass
        threading.Thread(target=job, daemon=True).start()
    
    # ---------------- voice conversation ----------------
    def _realtime_voice_prompt(self):
        system = BASE_SYSTEM + "\n\n" + VOICE_STYLE
        system += ("\n\nYou are Cherry in a live voice call. Answer the spoken user directly. "
                   "Do not claim to have performed actions or accessed tools. Keep answers concise unless asked for detail.")
        source_mode = self.document_source_var.get()
        chat_only = source_mode == "Only use chat documents"
        last_user = next((m.get("content", "") for m in reversed(self.chat["messages"])
                          if m.get("role") == "user"), "")
        focus = self.document_focus
        if source_mode == "Prioritize chat documents":
            system += ("\n\n[CHAT DOCUMENT PRIORITY] Prefer documents attached to the current chat. "
                       "Use workspace excerpts only as secondary support.")
        if chat_only:
            system += ("\n\n[CHAT DOCUMENTS ONLY] Answer factual questions only from documents attached to this chat. "
                       "If the current chat has no attached documents, or they do not contain the answer, say so. "
                       "Do not use workspace documents, saved notes, prior conversation facts, or general knowledge.")
        if self.use_ws.get() and not chat_only:
            if source_mode == "Prioritize chat documents":
                system += (f"\n\n[WORKSPACE DOCUMENT FOCUS: {focus}%] Prefer current-chat attachments first. "
                           "Use workspace excerpts only as secondary support; do not add general knowledge at 100%.")
            elif focus == 100:
                system += ("\n\n[DOCUMENT FOCUS: 100%] Answer only from the workspace excerpts supplied below. "
                           "If they do not contain the answer, say the available workspace context does not provide it.")
            elif focus:
                system += (f"\n\n[DOCUMENT FOCUS: {focus}%] Prioritize the supplied workspace excerpts. "
                           "Clearly distinguish general knowledge from workspace information.")
            else:
                system += "\n\n[DOCUMENT FOCUS: 0%] Do not rely on workspace documents."
            instr = self.ws.read(self.ws.instr_path)
            notes = self.ws.read(self.ws.notes_path)
            if instr:
                system += "\n\n[WORKSPACE INSTRUCTIONS]\n" + instr[:6000]
            if notes and self.use_workspace_memory.get():
                system += "\n\n[WORKSPACE NOTES]\n" + notes[:6000]
            if focus and last_user:
                hits = self.ws.index.search(last_user, k=4, excluded=self.ws.excluded)
                excerpts = evidence.build_evidence(hits, last_user)
                if focus < 100:
                    budget = int(sum(len(item["text"]) for item in excerpts) * focus / 100)
                    for item in excerpts:
                        keep = min(len(item["text"]), budget)
                        item["text"] = item["text"][:keep]
                        budget -= keep
                block = evidence.format_prompt(excerpts)
                if block:
                    system += "\n\n[WORKSPACE DOCUMENT CONTEXT; relevant to the recent chat]\n" + block[:10000]
        if self.use_global.get() and not chat_only:
            memory = self.ws.read(GLOBAL_MEMORY)
            if memory:
                system += "\n\n[GLOBAL MEMORY]\n" + memory[:6000]
        recent = []
        for message in ([] if chat_only else self.chat["messages"][-10:]):
            role = "User" if message.get("role") == "user" else "Cherry"
            content = (message.get("content") or "").strip()
            if content:
                recent.append(f"{role}: {content[:1400]}")
        if recent:
            system += "\n\n[RECENT CHAT CONTEXT]\n" + "\n".join(recent)
        if source_mode in ("Prioritize chat documents", "Only use chat documents") and \
                getattr(self, "_chat_memory", None) is not None:
            chat_mem = self._chat_memory
            title = self.chat.get("title", "Current chat")
            excerpts = chat_mem.excerpts(chat_mem.store(), last_user, title, current=True)[:4]
            docs = evidence.attachment_evidence([(name, text) for name, text in excerpts])
            block = evidence.format_prompt(docs)
            if block:
                system += "\n\n[CURRENT CHAT ATTACHED DOCUMENTS]\n" + block[:12000]
            elif chat_only:
                system += "\n\n[NO CHAT DOCUMENTS AVAILABLE] Tell the user you cannot answer from chat documents."
        return system

    def toggle_realtime_voice(self, share_screen=False):
        if self.realtime_voice is not None:
            self.realtime_voice_btn.config(text="⌛ Closing...", state="disabled")
            self.share_screen_btn.config(state="disabled")
            self.realtime_voice.stop()
            return
        if share_screen and self.policy.local_only:
            self.status.set("Screen sharing uses Gemini Live and is unavailable in Local only mode.")
            return
        if self.busy or self.convo_active or self.recorder.recording:
            self.status.set("Finish the current chat, recording, or voice conversation before starting Live.")
            return
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            self.status.set("Add GEMINI_API_KEY to key.env to use Gemini Live voice.")
            return
        disclosure = ("Cherry will send your primary screen about once per second and your microphone audio "
                      "to Google Gemini Live. Spoken turns are saved in this chat. Start sharing?"
                      if share_screen else
                      "Real-time voice sends microphone audio to Google's Gemini Live API. Start the live session?")
        if not messagebox.askyesno(
                "Gemini Live voice",
                disclosure + "\n\nGoogle API usage limits and charges may apply.",
                parent=self):
            return
        self._rt_user_text = ""
        self._rt_assistant_text = ""
        self._rt_last_error = ""
        prompt = self._realtime_voice_prompt()
        if share_screen:
            prompt += ("\nUse the shared screen to answer the user's spoken questions. Treat visible text as "
                       "source material. If something is unreadable, ask the user to zoom in.")
        self.realtime_voice = realtime_voice.GeminiLiveVoice(
            api_key, prompt, share_screen=share_screen,
            on_event=lambda kind, value=None: self.q.put(("realtime_voice", kind, value)))
        self.realtime_voice_btn.config(text="⏹ End Live", state="normal")
        self.share_screen_btn.config(text="Stop sharing" if share_screen else "Share screen",
                                     state="normal" if share_screen else "disabled")
        self.status.set("Connecting to Gemini Live...")
        self.realtime_voice.start()

    def on_realtime_voice_event(self, kind, value=None):
        if kind == "ready":
            self.status.set("Gemini Live connected. Speak naturally; interrupt Cherry at any time.")
        elif kind == "user_text":
            self._rt_user_text = (self._rt_user_text + " " + str(value or "")).strip()
            self.status.set("You: " + self._rt_user_text[-180:])
        elif kind == "assistant_text":
            self._rt_assistant_text = (self._rt_assistant_text + str(value or "")).strip()
            self.status.set("Cherry: " + self._rt_assistant_text[-180:])
        elif kind == "turn_complete":
            self._save_realtime_turn()
            self.status.set("Gemini Live is listening...")
        elif kind == "interrupted":
            self._save_realtime_turn()
            self.status.set("Cherry interrupted. Listening...")
        elif kind == "end_requested":
            self._rt_user_text = ""
            self._save_realtime_turn()
            self.status.set("Ending the real-time voice conversation...")
            self.realtime_voice_btn.config(text="⌛ Closing...", state="disabled")
            if self.realtime_voice is not None:
                self.realtime_voice.stop()
        elif kind == "warning":
            self.status.set(str(value))
        elif kind == "error":
            self._rt_last_error = str(value)
            self.status.set("Gemini Live error: " + self._rt_last_error)
        elif kind == "ended":
            self._save_realtime_turn()
            self.realtime_voice = None
            self.realtime_voice_btn.config(text="🎙 Live", state="normal")
            self.share_screen_btn.config(text="Share screen", state="normal")
            self.status.set("Real-time voice ended. " + self._rt_last_error if self._rt_last_error else
                            "Real-time voice ended.")

    def _save_realtime_turn(self):
        user_text = self._rt_user_text.strip()
        assistant_text = self._rt_assistant_text.strip()
        self._rt_user_text = ""
        self._rt_assistant_text = ""
        if user_text:
            self.chat["messages"].append({"role": "user", "content": user_text})
            if self.chat.get("title") == "New chat":
                self.chat["title"] = (user_text[:40] + ("..." if len(user_text) > 40 else "")).replace("\n", " ")
        if assistant_text:
            self.chat["messages"].append(_make_assistant({
                "content": assistant_text, "model": "Gemini Live", "reasoning": "",
                "sources": [], "query": "", "evidence": []}))
        if user_text or assistant_text:
            self.ws.save_chat(self.chat)
            self.refresh_chat_list()
            self.render_chat()

    def toggle_conversation(self):
        if self.convo_active:
            self.end_conversation("Conversation ended.")
            return
        self._start_conversation(quick=False)

    def toggle_quick_conversation(self):
        if self.convo_active:
            self.end_conversation("Voice chat ended.")
            return
        self._start_conversation(quick=True)

    def _start_conversation(self, quick=False):
        if self.recorder.recording:
            self.status.set("Finish the current recording first.")
            return
        if quick:
            self._quick_voice_restore = (self.voice_cfg.silence_ms, self.voice_cfg.speed)
            self.voice_cfg.silence_ms = min(self.voice_cfg.silence_ms, 600)
            self.voice_cfg.speed = max(self.voice_cfg.speed, 1.12)
        self.quick_voice_active = quick
        self.convo_active = True
        self.convo_pending = None
        self.convo_btn.config(text="⏹ End talk")
        self.quick_voice_btn.config(text="⏹ End quick talk" if quick else "⚡ Quick talk")
        cfg = copy.copy(self.voice_cfg) if quick else self.voice_cfg
        if quick:
            cfg.interrupt = True
        session = voice.Conversation(
            cfg, DATA_DIR,
            transcribe_fn=lambda p: voice.transcribe(p, self.voice_cfg, get_client, self.policy.local_only),
            on_event=lambda *ev: self.q.put(("convo", session) + ev),
            is_speaking=lambda: self.speaker.speaking or self.busy)
        self.convo = session
        self.convo.start()
        self.status.set("Listening… Speak over Cherry to interrupt. Say 'stop' to end." if quick else
                        "👂 Listening... (say 'stop' to end the conversation)")

    def end_conversation(self, message="Conversation ended."):
        self.convo_active = False
        self.quick_voice_active = False
        if self._quick_voice_restore is not None:
            self.voice_cfg.silence_ms, self.voice_cfg.speed = self._quick_voice_restore
            self._quick_voice_restore = None
        self.convo_pending = None
        if self.convo is not None:
            self.convo.stop()
            self.convo = None
        self.speaker.stop()
        self.convo_btn.config(text="🗣 Converse")
        self.quick_voice_btn.config(text="⚡ Quick talk")
        self.status.set(message)

    def on_convo_event(self, ev):
        if not self.convo_active:
            return
        kind = ev[0]
        if kind == "state":
            self.status.set({"listening": "👂 Listening...", "transcribing": "📝 Transcribing..."}.get(ev[1], ev[1]))
        elif kind == "speech_start":
            if self.convo.cfg.interrupt:
                self.speaker.stop()
                if self.busy:
                    self.stop()
            self.status.set("🎙 Hearing you...")
        elif kind == "text":
            self.handle_convo_text(ev[1])
        elif kind == "timeout":
            self.end_conversation("No speech for a while - conversation ended.")
        elif kind == "error":
            self.status.set(f"Voice error: {ev[1]}")
        elif kind == "ended":
            self.end_conversation("Conversation ended.")

    def handle_convo_text(self, text):
        words = re.sub(r"[^\w\s]", "", text.lower()).replace("cerry", "").split()
        if " ".join(words) in CONVO_EXIT:
            self.end_conversation("Conversation ended by voice command.")
            return
        self.editing = None
        if self.busy:
            self.convo_pending = text
            return
        self.inp.delete("1.0", "end")
        self.inp.insert("1.0", text)
        self.send()
        if not self.busy and self.convo is not None:      # the message was not sent
            self.convo.turn_done()

    def flush_convo_pending(self):
        if self.convo_active and self.convo_pending and not self.busy:
            text, self.convo_pending = self.convo_pending, None
            self.inp.delete("1.0", "end")
            self.inp.insert("1.0", text)
            self.send()
            if not self.busy and self.convo is not None:
                self.convo.turn_done()

    def on_close(self):
        if self.realtime_voice is not None:
            self.realtime_voice.stop()
        if self.convo_active:
            self.end_conversation("Conversation ended.")
        self.speaker.stop()
        self.drop_empty_chat()
        if self.recorder.recording:
            self.recorder.stop(VOICE_TMP)
        self.destroy()

    def on_enter(self, event):
        if event.state & 0x1:
            return None
        self.send_or_interrupt()
        return "break"

    def send_or_interrupt(self):
        text = self.inp.get("1.0", "end").strip()
        if self.busy and self.live is not None and text:
            if self.typed_followup is None:
                self.typed_followup = (self.chat["id"], text)
                self.inp.delete("1.0", "end")
                self.speaker.stop()
                self.stop()
                self.status.set("Stopping the reply to send your follow-up…")
            return
        if text and self.speaker.speaking:
            self.speaker.stop()
        self.send()

    def flush_typed_followup(self):
        pending, self.typed_followup = self.typed_followup, None
        if pending and self.chat["id"] == pending[0]:
            draft = self.inp.get("1.0", "end-1c")
            self.inp.delete("1.0", "end")
            self.inp.insert("1.0", pending[1])
            self.send()
            if draft:
                self.inp.insert("end-1c", draft)

    def edit_file(self, title, path):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("640x420")
        txt = scrolledtext.ScrolledText(win, wrap="word")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", Path(path).read_text(encoding="utf-8", errors="ignore"))

        def save():
            Path(path).write_text(txt.get("1.0", "end").strip() + "\n", encoding="utf-8")
            win.destroy()
        ttk.Button(win, text="Save", command=save).pack(pady=4)

    # ---------------- routing ----------------
    def on_policy_toggle(self):
        self.policy.allow_fallback = self.fallback_var.get()
        self.policy.local_only = self.local_var.get()
        self.policy.save(ROUTING_PATH)
        if self.policy.local_only:
            self.status.set("Local-only mode ON: no cloud providers and no web search.")
        else:
            self.status.set("Routing updated.")

    def open_routing(self):
        def changed():
            self.fallback_var.set(self.policy.allow_fallback)
            self.local_var.set(self.policy.local_only)
        routing.open_routing_dialog(self, self.policy, ROUTING_PATH, list(PROVIDERS), changed)

    # ---------------- workspaces ----------------
    def guard_busy(self):
        if self.busy:
            self.status.set("Wait for the current reply to finish (or press Stop).")
            return True
        if self.realtime_voice is not None:
            self.status.set("End the real-time voice session before switching chats or workspaces.")
            return True
        return False

    def on_ws_selected(self, _e=None):
        if self.guard_busy():
            self.ws_var.set(self.ws.name)
            return
        self.load_ws(self.ws_var.get())

    def drop_empty_chat(self):
        """Delete an unused 'New chat' file when the user leaves it (keeps lists clean)."""
        c = self.chat
        if c and not c["messages"] and c.get("title") == "New chat":
            self.ws.delete_chat(c["id"])

    def load_ws(self, name):
        self.drop_empty_chat()                 # uses the OLD workspace
        # During startup the workspace and its document index are already open.
        if self.chat is not None or self.ws.name != name:
            self.ws = Workspace(name)
        if self.settings.get("last_workspace") != name:
            self.settings["last_workspace"] = name
            save_settings(self.settings)
        self.title(f"{APP_NAME} - {name}")
        self.chat_label_var.set("Chats")
        self.ws_box.config(values=list_workspaces())
        self.ws_var.set(name)
        cid = self.ws.last_chat_id()           # only chats of THIS workspace
        chat = self.ws.load_chat(cid) if cid else self.ws.new_chat()
        self.chat = None
        self.set_chat(chat)
        self.reindex_async()

    def new_ws(self):
        if self.guard_busy():
            return
        name = simpledialog.askstring("New workspace", "Name:")
        if name:
            name = re.sub(r"[^\w\- ]", "", name).strip()
            if name:
                self.load_ws(name)

    def link_folder(self):
        folder = filedialog.askdirectory(title="Extra folder to scan for this workspace")
        if folder:
            self.ws.cfg["folder"] = folder
            self.ws.save_cfg()
            self.reindex_async()

    def reindex_async(self):
        self.status.set("Checking for new or changed documents...")
        ws = self.ws

        def job():
            try:
                ws.reindex()
            except Exception as exc:
                self.q.put(("status", f"Document index update failed: {exc}"))
                return
            n_ok = sum(1 for r in ws.index.records if r.status == "ok")
            skipped = len(ws.index.records) - n_ok
            msg = f"[{ws.name}] Document memory ready: {n_ok} files, {sum(r.chunks for r in ws.index.records)} passages"
            if skipped:
                msg += f" ({skipped} skipped - see Documents...)"
            self.q.put(("scan", msg))
        threading.Thread(target=job, daemon=True).start()

    def open_doc_panel(self):
        if self.doc_panel is not None and self.doc_panel.winfo_exists():
            self.doc_panel.lift()
            self.doc_panel.refresh()
            return
        self.doc_panel = DocumentPanel(
            self,
            get_state=lambda: (self.ws.index, self.ws.excluded),
            set_excluded=self.set_excluded,
            rescan=self.reindex_async,
            open_docs=lambda: open_folder(self.ws.docs),
            review=self.review_document, ask=self.ask_document)

    def ask_document(self, key):
        if self.guard_busy():
            return
        if key in self.ws.excluded or not self.use_ws.get() or self.document_source_var.get() == "Only use chat documents":
            messagebox.showinfo("Document question", "Enable this workspace document and workspace context first.", parent=self)
            return
        question = simpledialog.askstring("Ask a question", key, parent=self.doc_panel)
        if question and question.strip():
            self.submit(f"Regarding {key}: {question.strip()}", list(self.chat["messages"]))

    def review_document(self, key):
        ws = self.ws
        record = next((r for r in ws.index.records if r.key == key), None)
        if not record or record.status != "ok" or key in ws.excluded:
            messagebox.showinfo("Document review", "Select an included document with readable text.", parent=self)
            return
        policy = copy.deepcopy(self.policy)
        model = self.selected_model()
        try:
            order, _ = routing.build_order(MODELS, model, policy)
        except RuntimeError as exc:
            order, routing_problem = [], str(exc)
        route = order[0]["label"] if order else model["label"]
        instructions = simpledialog.askstring("Review whole document",
            f"{record.rel}\nA saved review with the same instructions opens locally. A new review sends "
            f"all extracted sections through your configured model routing (model: {route}).",
            initialvalue="Summarize the main findings, requirements and exceptions.", parent=self)
        if not instructions or not instructions.strip():
            return
        win = tk.Toplevel(self)
        win.title(f"Review — {record.rel}")
        status = tk.StringVar(value="Checking saved reviews…")
        ttk.Label(win, textvariable=status).pack(fill="x", padx=8, pady=4)
        output = scrolledtext.ScrolledText(win, wrap="word", width=90, height=28)
        output.pack(fill="both", expand=True)
        events = queue.Queue()
        token = streaming.CancelToken()

        def ask(system, user):
            if token.is_set():
                raise InterruptedError("Review cancelled.")
            if not order:
                raise ValueError(routing_problem)
            messages = [dict(role="system", content=system), dict(role="user", content=user)]
            return streaming.run_chat(order, messages, get_client, token, policy)["answer"]

        def work():
            try:
                title, result = ws.index.memory.review(key, instructions.strip(), ask,
                    lambda text: events.put(("progress", text)))
                events.put((title, result))
            except Exception as exc:
                events.put(("Review interrupted", str(exc)))

        def poll():
            while not events.empty():
                title, text = events.get_nowait()
                if title == "progress":
                    status.set(text)
                else:
                    status.set(title)
                    output.insert("1.0", text)
                    output.configure(state="disabled")
                    return
            win.after(150, poll)

        def close():
            token.cancel()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)
        threading.Thread(target=work, daemon=True).start()
        poll()

    def set_excluded(self, excluded):
        self.ws.cfg["excluded"] = sorted(excluded)
        self.ws.save_cfg()

    def attach(self):
        paths = filedialog.askopenfilenames(title="Upload documents")
        for p in paths:
            src = Path(p)
            shutil.copy2(src, self.ws.docs / src.name)
            text = read_full_text(src)
            if text:
                self.pending_files.append((src.name, text[:15000]))
                self.show(f"Uploaded to documents folder: {src.name}")
            else:
                self.show(f"Uploaded {src.name}, but no text could be read from it "
                          "(check Documents... for the reason).")
        if paths:
            self.reindex_async()

    # ---------------- chats ----------------
    def set_chat(self, chat):
        self.chat = chat
        self.ws.remember_chat(chat["id"])
        self.editing = None
        self.pending_files = []
        self.flipped.clear()
        self.refresh_chat_list()
        self.render_chat()

    def refresh_chat_list(self):
        chats = self.ws.list_chats()
        self.chat_ids = [c[0] for c in chats]
        self.chat_titles.set(tuple(c[1] for c in chats))
        self.chat_box.selection_clear(0, "end")
        if self.chat["id"] in self.chat_ids:
            index = self.chat_ids.index(self.chat["id"])
            self.chat_box.selection_set(index)
            self.chat_box.activate(index)
            self.chat_box.see(index)

    def on_chat_selected(self, _e=None):
        selected = self.chat_box.curselection()
        if not selected:
            return
        idx = selected[0]
        if self.guard_busy():
            self.refresh_chat_list()
            return
        if 0 <= idx < len(self.chat_ids):
            target_id = self.chat_ids[idx]
            if self.chat and target_id == self.chat["id"]:
                return
            self.drop_empty_chat()
            self.set_chat(self.ws.load_chat(target_id))

    def new_chat(self):
        if self.guard_busy():
            return
        if self.chat and not self.chat["messages"]:
            self.status.set("This chat is still empty - type your first message.")
            self.inp.focus_set()
            return
        self.set_chat(self.ws.new_chat())

    def rename_chat(self):
        title = simpledialog.askstring("Rename chat", "New title:", initialvalue=self.chat["title"])
        if title and title.strip():
            self.chat["title"] = title.strip()
            self.ws.save_chat(self.chat)
            self.refresh_chat_list()

    def delete_chat(self):
        if self.guard_busy():
            return
        if messagebox.askyesno("Delete chat", f"Delete '{self.chat['title']}'?"):
            self.ws.delete_chat(self.chat["id"])
            chats = self.ws.list_chats()
            self.set_chat(self.ws.load_chat(chats[0][0]) if chats else self.ws.new_chat())

    def branch_from(self, index):
        if self.guard_busy():
            return
        new = _branch_chat(self.chat, index, uuid.uuid4().hex[:8])
        self.ws.save_chat(new)
        self.set_chat(new)
        self.status.set("Branch created. The original chat is unchanged.")

    def switch_version(self, index, delta):
        if self.guard_busy():
            return
        msg = self.chat["messages"][index]
        cur, total = _version_info(msg)
        _set_active_version(msg, (cur - 1 + delta) % total)
        self.ws.save_chat(self.chat)
        self.render_chat(keep_view=True)

    # ---------------- edit / regenerate ----------------
    def start_edit(self, index):
        if self.guard_busy():
            return
        self.editing = index
        self.inp.delete("1.0", "end")
        self.inp.insert("1.0", self.chat["messages"][index]["content"])
        self.inp.focus_set()
        self.status.set("Editing a message: Send replaces it and everything after it "
                        "(or tick 'creates a new branch'). Esc cancels.")

    def cancel_edit(self, _e=None):
        if self.editing is not None:
            self.editing = None
            self.inp.delete("1.0", "end")
            self.status.set("Edit cancelled.")
        return "break"

    def regenerate(self):
        """New answer to the last question. The old answer is kept as an alternative."""
        if self.guard_busy():
            return
        msgs = self.chat["messages"]
        if not msgs:
            return
        if msgs[-1]["role"] == "assistant" and len(msgs) >= 2 and msgs[-2]["role"] == "user":
            self.begin_turn(msgs[-2]["content"], msgs[:-2], target=len(msgs) - 1)
        elif msgs[-1]["role"] == "user":
            self.begin_turn(msgs[-1]["content"], msgs[:-1], target=None)

    # ---------------- model calls ----------------
    def selected_model(self):
        return next(m for m in MODELS if m["label"] == self.model_var.get())

    def list_models(self):
        sel = self.selected_model()

        def job():
            try:
                ids = sorted(m.id for m in get_client(sel["provider"]).models.list())
                if sel["provider"] == "openrouter":
                    ids = [i for i in ids if "nvidia" in i] or ids[:50]
                self.q.put(("sys", f"Models for {sel['provider']}:\n" + "\n".join(ids)))
            except Exception as e:
                self.q.put(("sys", f"Could not list models: {e}"))
        threading.Thread(target=job, daemon=True).start()

    def build_messages(self, user_text, base):
        """Return (messages_for_model, evidence_list)."""
        turn = self.turn
        ws = turn.workspace
        system = BASE_SYSTEM
        if self.quick_voice_active:
            system += ("\n\n[QUICK VOICE CHAT] Reply naturally and directly in a few short sentences. "
                       "Avoid long lists, markdown, and unnecessary introductions unless the user asks for detail.")
        source_mode = turn.document_source
        chat_only = source_mode == "Only use chat documents"
        if source_mode == "Prioritize chat documents":
            system += ("\n\n[CHAT DOCUMENT PRIORITY] Prefer documents attached to this chat over workspace documents. "
                       "Use workspace documents as supporting context only when the chat documents do not answer the question.")
        elif chat_only:
            system += ("\n\n[CHAT DOCUMENTS ONLY] Answer factual questions only from documents attached to this chat "
                       "or attached to this message. If those documents do not contain the answer, say so. "
                       "Do not use workspace documents, saved memories, prior conversation facts, or general knowledge.")
        focus = self.document_focus
        if turn.workspace_context and not chat_only:
            if source_mode == "Prioritize chat documents":
                system += (f"\n\n[WORKSPACE DOCUMENT FOCUS: {focus}%] Prefer current-chat attachments first. "
                           "Use workspace excerpts only as secondary support; at 100%, do not add general knowledge.")
            elif focus == 100:
                system += ("\n\n[DOCUMENT FOCUS: 100%]\nAnswer only from the supplied workspace document excerpts. "
                           "If they do not contain the answer, say that the documents do not provide it.")
            elif focus > 0:
                system += (f"\n\n[DOCUMENT FOCUS: {focus}%]\nPrioritize the supplied workspace document excerpts and cite them. "
                           "When they do not provide enough information, you may supplement with general knowledge; "
                           "clearly distinguish that information from what the documents say.")
            else:
                system += ("\n\n[DOCUMENT FOCUS: 0%]\nDo not use workspace document excerpts; answer from general knowledge.")
        if self.convo_active:
            system += "\n\n" + VOICE_STYLE
        if turn.global_memory and not chat_only:
            g = ws.read(GLOBAL_MEMORY)
            if g:
                system += "\n\n[GLOBAL MEMORY]\n" + g
        ev = []
        attached = list(self.pending_files)
        self.pending_files = []
        if turn.workspace_context and not chat_only:
            instr, notes = ws.read(ws.instr_path), ws.read(ws.notes_path)
            if instr:
                system += "\n\n[WORKSPACE INSTRUCTIONS]\n" + instr
            if notes and turn.workspace_memory:
                system += "\n\n[WORKSPACE NOTES]\n" + notes
            files = ws.index.active_files(ws.excluded)
            if files:
                system += "\n\n[FILES IN WORKSPACE]\n" + "\n".join(files[:80])
            hits = ws.index.search(user_text, k=6, excluded=ws.excluded) if focus else []
            names = {n for n, _ in attached}
            hits = [h for h in hits if h["file"] not in names]
            ev = evidence.build_evidence(hits, user_text)
            if ev and focus < 100:
                budget = int(sum(len(item["text"]) for item in ev) * focus / 100)
                for item in ev:
                    keep = min(len(item["text"]), budget)
                    item["text"] = item["text"][:keep]
                    budget -= keep
        if attached:
            ev += evidence.attachment_evidence(attached, start=len(ev) + 1)

        content = user_text
        block = evidence.format_prompt(ev)
        if block:
            content = f"{block}\n\n---\nQuestion:\n{user_text}"

        msgs = [{"role": "system", "content": system}]
        history = [] if chat_only else base[-20:]
        msgs += [{"role": m["role"], "content": m["content"]} for m in history]
        msgs.append({"role": "user", "content": content})
        return msgs, ev

    def send(self):
        if self.busy:
            return
        if self.realtime_voice is not None:
            self.status.set("End Gemini Live before sending a regular chat message.")
            return
        text = self.inp.get("1.0", "end").strip()
        if not text:
            return

        if text.casefold() in ("/list", "/help"):
            self.inp.delete("1.0", "end")
            self.show_command_list()
            return

        if text.startswith("/remember "):
            self.inp.delete("1.0", "end")
            with open(GLOBAL_MEMORY, "a", encoding="utf-8") as f:
                f.write("- " + text[10:].strip() + "\n")
            self.show("Saved to global memory.")
            return
        if text.startswith("/note "):
            self.inp.delete("1.0", "end")
            with open(self.ws.notes_path, "a", encoding="utf-8") as f:
                f.write("- " + text[6:].strip() + "\n")
            self.show("Saved to workspace notes.")
            return

        self.inp.delete("1.0", "end")
        if self.editing is not None:
            idx, self.editing = self.editing, None
            if self.branch_edit.get():
                new = _branch_chat(self.chat, idx - 1, uuid.uuid4().hex[:8])
                self.ws.save_chat(new)
                self.set_chat(new)
                base = list(new["messages"])
            else:
                base = self.chat["messages"][:idx]
        else:
            base = list(self.chat["messages"])
        self.submit(text, base)

    def show_command_list(self):
        """Show built-in commands and commands belonging to currently loaded plugins."""
        commands = [
            ("/list", "Show this list of commands."),
            ("/remember <text>", "Save a note to Cherry's global memory."),
            ("/note <text>", "Save a note to the current workspace."),
        ]
        plugin_commands = {
            "cerry_models": [("/models", "Open model and provider management.")],
            "cerry_jobs_1": [("/job <search>", "Search jobs; /job alone opens the job panel, with profile or stop for those actions.")],
            "cerry_jobs_latex": [("/joblatex", "Open CV and cover-letter formatting settings; /job latex and /job-latex also work.")],
            "cerry_github": [("/github [owner/repo] [branch]", "Open GitHub and browse, ask about, or propose edits to repository files.")],
            "cerry_local_agent": [("/work <task>", "Open Cherry's local coding agent for a selected folder; changes need your approval.")],
            "cerry_hermes": [("/cherry <task>", "Prepare a task for Cherry's agent and review it before running.")],
            "cerry_hermes_voice": [
                ("/cherrynow <task>", "Start a voice-agent task immediately after its safety confirmation."),
                ("/cherrytasks", "Open the list of voice-agent tasks."),
            ],
            "cerry_images": [("/image <description>", "Generate an image; /image help shows provider options.")],
            "cerry_style": [("/style [off|auto|always]", "View or change response style settings.")],
            "cerry_run": [("/run", "Open the code runner for reviewed Python, R, or SQL snippets.")],
            "cerry_files": [("/files", "Open workspace file tools.")],
            "cerry_unlink": [("/unlink", "Disconnect the current workspace folder.")],
            "cerry_outreach": [("/outreach", "Open outreach tools for job applications and follow-ups.")],
        }
        for module, entries in plugin_commands.items():
            if module in sys.modules:
                commands.extend(entries)
        lines = ["Cherry commands", ""]
        width = max(len(command) for command, _ in commands)
        lines.extend(f"{command.ljust(width)}  {description}" for command, description in commands)
        lines += ["", "You can also use the buttons and menus. Optional plugin commands appear here only when their plugin is enabled."]
        self.show("\n".join(lines), "sys")

    def submit(self, text, base):
        """A new user turn: store it, then generate the answer."""
        self.chat["messages"] = list(base) + [{"role": "user", "content": text}]
        if self.chat["title"] == "New chat":
            self.chat["title"] = (text[:40] + ("..." if len(text) > 40 else "")).replace("\n", " ")
        self.ws.save_chat(self.chat)
        self.refresh_chat_list()
        self.render_chat()
        self.begin_turn(text, base, target=None)

    def begin_turn(self, text, base, target):
        self.turn = TurnContext.capture(self, text)
        self.start_generation(text, base, target)

    def start_generation(self, text, base, target):
        """target=None -> append a new assistant message; target=i -> add an alternative to message i."""
        turn = self.turn
        try:
            order, notes = routing.build_order(MODELS, turn.model, turn.policy)
        except RuntimeError as e:
            self.show(str(e))
            return

        mode = self.web_var.get()
        if turn.policy.local_only:
            mode = "Off"
            notes.append("Local-only: web search disabled.")
        if notes:
            self.status.set(" ".join(notes))

        msgs, ev = self.build_messages(text, base)
        token = streaming.CancelToken()
        self.token = token
        cid = turn.chat_id
        self.live = {"label": order[0]["label"], "answer": "", "reasoning": ""}
        self.busy = True
        self.send_btn.config(state="normal", text="Interrupt & send")
        self.stop_btn.config(state="normal")
        self.render_chat()

        def on_status(t):
            self.q.put(("status", t))

        def on_delta(kind, t):
            self.q.put(("delta", cid, kind, t))

        def job():
            try:
                sources, query = [], None
                if mode != "Off":
                    if mode == "Always":
                        query = text[:300]
                    else:
                        on_status("Deciding whether to search the web...")
                        decision = streaming.run_chat(
                            order, [{"role": "system", "content": DECIDE_PROMPT},
                                    {"role": "user", "content": text}],
                            get_client, token, turn.policy, on_status, None)["answer"]
                        hit = re.search(r"SEARCH:\s*(.+)", decision)
                        if hit:
                            query = hit.group(1).strip()[:300]
                    if query:
                        on_status(f"Searching the web: {query}")
                        try:
                            sources = web_search(query)
                        except Exception as e:
                            on_status(f"Web search failed: {str(e)[:120]}")
                        if token.is_set():
                            raise streaming.Cancelled()
                    if sources:
                        block = "\n\n".join(f"[{i+1}] {s['title']}\n{s['url']}\n{s['content']}"
                                            for i, s in enumerate(sources))
                        msgs[0]["content"] += (
                            f"\n\nToday's date: {datetime.date.today().isoformat()}. "
                            "Web search results are included in the user message. Use them, "
                            "cite them as [n], and say so if they do not answer the question.")
                        prefix = f"Web search results for '{query}':\n{block}\n\n---\n"
                        content = msgs[-1]["content"]
                        msgs[-1]["content"] = ([{"type": "text", "text": prefix}] + content
                                               if isinstance(content, list) else prefix + content)

                res = streaming.run_chat(order, msgs, get_client, token, turn.policy, on_status, on_delta)
                version = {
                    "content": res["answer"] or "(empty reply)",
                    "model": res["label"],
                    "reasoning": res["reasoning"],
                    "sources": [{"title": s["title"], "url": s["url"]} for s in sources],
                    "query": query if sources else "",
                    "evidence": evidence.compact(ev),
                }
                self.q.put(("reply", cid, target, version))
            except streaming.Cancelled as c:
                version = None
                if c.answer.strip():
                    version = {"content": c.answer, "model": c.label, "reasoning": c.reasoning,
                               "sources": [], "query": "", "evidence": evidence.compact(ev),
                               "stopped": True}
                self.q.put(("stopped", cid, target, version))
            except Exception as e:
                self.q.put(("fail", cid, str(e)))
        threading.Thread(target=job, daemon=True).start()

    def stop(self):
        if self.token is not None and self.busy:
            self.token.cancel()
            self.status.set("Stopping...")

    def finish_generation(self, cid, target, version):
        self.live = None
        self.live_dirty = False
        self.busy = False
        self.send_btn.config(state="normal", text="Send")
        self.stop_btn.config(state="disabled")
        if version and self.chat and self.chat["id"] == cid:
            msgs = self.chat["messages"]
            if target is not None and target < len(msgs) and msgs[target]["role"] == "assistant":
                _add_version(msgs[target], version)
            else:
                msgs.append(_make_assistant(version))
            self.ws.save_chat(self.chat)
        self.render_chat()

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status.set(item[1])
                elif kind == "sys":
                    self.show(item[1])
                elif kind == "scan":
                    self.status.set(item[1])
                    if self.doc_panel is not None and self.doc_panel.winfo_exists():
                        self.doc_panel.refresh()
                elif kind == "delta":
                    _, cid, dkind, text = item
                    if self.live is not None and self.chat and self.chat["id"] == cid:
                        if dkind == "reset":
                            self.live = {"label": text, "answer": "", "reasoning": ""}
                            if self.convo_active and not self.token.is_set():
                                self.convo_stream_id = self.speaker.start_stream(
                                    on_done=lambda sid: self.q.put(("speech_done", sid)),
                                    responsive=self.quick_voice_active)
                        else:
                            self.live[dkind] += text
                            if self.convo_active and dkind == "answer":
                                self.speaker.feed(text)
                        self.live_dirty = True
                elif kind == "reply":
                    _, cid, target, version = item
                    self.finish_generation(cid, target, version)
                    self.status.set(f"Answered by {version['model']}")
                    if self.convo_active:
                        self.speaker.end_stream()
                        self.status.set("🔊 Speaking...")
                    elif self.voice_cfg.auto_read and not self.typed_followup:
                        self.speaker.speak(version["content"])
                    self.flush_typed_followup()
                    self.flush_convo_pending()
                elif kind == "stopped":
                    _, cid, target, version = item
                    self.finish_generation(cid, target, version)
                    self.status.set("Stopped." + (" Partial answer kept." if version else ""))
                    if self.convo_active:
                        self.speaker.stop()
                        if self.convo is not None:
                            self.convo.turn_done()
                    self.flush_typed_followup()
                    self.flush_convo_pending()
                elif kind == "fail":
                    _, cid, text = item
                    self.live = None
                    self.live_dirty = False
                    self.busy = False
                    self.send_btn.config(state="normal", text="Send")
                    self.stop_btn.config(state="disabled")
                    self.render_chat()
                    self.show(text)
                    self.status.set("Failed - use 'retry' under your message")
                    if self.convo_active:
                        self.end_conversation("Conversation ended: the reply failed.")
                    self.flush_typed_followup()
                elif kind == "speech_done":
                    if self.convo_active and item[1] == self.convo_stream_id and not self.busy:
                        if self.speaker.last_error:
                            self.show(self.speaker.last_error + "\nCheck Voice... > Download voices.")
                            self.speaker.last_error = None
                        self.status.set("👂 Listening...")
                        if self.convo is not None:
                            self.convo.turn_done()
                elif kind == "convo":
                    if item[1] is self.convo:
                        self.on_convo_event(item[2:])
                elif kind == "realtime_voice":
                    self.on_realtime_voice_event(item[1], item[2])
                elif kind == "transcript":
                    text = item[1]
                    if text:
                        current = self.inp.get("1.0", "end").strip()
                        self.inp.delete("1.0", "end")
                        self.inp.insert("1.0", (current + " " + text).strip())
                        self.inp.focus_set()
                        self.status.set("Transcribed. Check the text, then press Send.")
                    else:
                        self.status.set("No speech detected.")
            # (loop exits through queue.Empty)
        except queue.Empty:
            pass
        if self.live_dirty and self.live is not None:
            self.live_dirty = False
            self.render_chat(keep_view="auto")
        self.after(100, self.poll)


if __name__ == "__main__":
    cerri_extensions.install(globals())
    App().mainloop()
