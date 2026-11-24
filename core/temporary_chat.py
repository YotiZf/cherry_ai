"""A text conversation whose history lives only in its own window."""
from copy import deepcopy
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

import routing
import streaming


class TemporaryChat(tk.Toplevel):
    def __init__(self, app, models, get_client):
        super().__init__(app)
        self.title("Cherry — Temporary chat")
        self.geometry("720x560")
        self.models = models
        self.get_client = get_client
        self.messages = []
        self.token = None
        self.timer = None
        self.events = queue.Queue()
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Closing this window discards the conversation. Cloud providers may retain requests.",
                  wraplength=640).pack(anchor="w", pady=(0, 8))
        self.history = scrolledtext.ScrolledText(body, state="disabled", wrap="word", height=14)
        self.history.pack(fill="both", expand=True)
        self.entry = tk.Text(body, height=4, wrap="word")
        self.entry.pack(fill="x", pady=8)
        self.entry.bind("<Return>", self.enter)
        bar = ttk.Frame(body)
        bar.pack(fill="x")
        self.status = ttk.Label(bar, text=app.selected_model()["label"])
        self.status.pack(side="left", fill="x", expand=True)
        self.send_button = ttk.Button(bar, text="Send", command=self.send)
        self.send_button.pack(side="right")
        self.entry.focus_set()

    def enter(self, event):
        if event.state & 1:  # Shift+Enter inserts a newline.
            return
        self.send()
        return "break"

    def append(self, speaker, text):
        self.history.configure(state="normal")
        self.history.insert("end", f"{speaker}\n{text}\n\n")
        self.history.configure(state="disabled")
        self.history.see("end")

    def send(self):
        text = self.entry.get("1.0", "end-1c").strip()
        if self.token or not text:
            return
        policy = deepcopy(self.master.policy)
        try:
            order, _ = routing.build_order(self.models, self.master.selected_model(), policy)
        except RuntimeError as exc:
            self.status.configure(text=str(exc))
            return
        self.messages.append({"role": "user", "content": text})
        self.append("You", text)
        self.entry.delete("1.0", "end")
        self.send_button.state(["disabled"])
        self.status.configure(text=f"Waiting for {order[0]['label']}…")
        self.token = token = streaming.CancelToken()
        messages = self.messages.copy()
        events, get_client = self.events, self.get_client

        def reply():
            try:
                result = streaming.run_chat(order, messages, get_client, token, policy)
                events.put((result["label"], result["answer"], None))
            except Exception as exc:
                events.put((None, None, str(exc)))

        threading.Thread(target=reply, daemon=True).start()
        self.timer = self.after(80, self.poll)

    def poll(self):
        try:
            label, answer, error = self.events.get_nowait()
        except queue.Empty:
            self.timer = self.after(80, self.poll)
            return
        self.timer = self.token = None
        self.send_button.state(["!disabled"])
        if error is not None:
            self.append("Request failed", error)
        else:
            self.messages.append({"role": "assistant", "content": answer})
            self.append(label, answer)
        self.status.configure(text="Temporary chat")

    def destroy(self):
        if self.timer is not None:
            self.after_cancel(self.timer)
        if self.token:
            self.token.cancel()
        self.messages.clear()
        super().destroy()
