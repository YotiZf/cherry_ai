"""check_local_llm.py - finds out why Cerry cannot reach your local model.

Run in PowerShell from your Cerry folder (AnythingLLM must be open):
    python check_local_llm.py
Optional: python check_local_llm.py --base http://127.0.0.1:11434/v1 --model NAME --key KEY

Step 1 looks for local servers that speak the OpenAI protocol (the way Cerry talks to models).
Step 2 sends one real question and measures how long the first word takes, because Cerry stops
waiting after its "First response deadline" (30 seconds by default).
Nothing is changed and no key is printed.
"""
import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

import httpx

PORTS = [(11434, "Ollama"), (1234, "LM Studio"), (3001, "AnythingLLM developer API"),
         (8000, "Lemonade or other"), (8080, "llama.cpp server"), (5000, "other"), (13305, "other")]
PATHS = ["/v1", "/api/v1", "/api/v1/openai"]


def load_env():
    for name in ("key.env", ".env"):
        p = Path(name)
        if p.is_file():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), 0.6):
            return True
    except OSError:
        return False


def list_models(base, key):
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        r = httpx.get(base + "/models", headers=headers, timeout=5)
    except httpx.HTTPError as e:
        return None, type(e).__name__
    if r.status_code != 200:
        return None, "HTTP %d" % r.status_code
    try:
        data = r.json().get("data", [])
        return [m.get("id", "?") for m in data], ""
    except ValueError:
        return None, "not JSON"


def scan(key):
    found = []
    print("Step 1: looking for local model servers\n")
    for port, name in PORTS:
        if not listening(port):
            print("  port %-5d %-26s nothing is listening" % (port, name))
            continue
        print("  port %-5d %-26s LISTENING" % (port, name))
        for path in PATHS:
            base = "http://127.0.0.1:%d%s" % (port, path)
            models, why = list_models(base, key)
            if models is not None:
                print("      %s -> %d model(s): %s" % (base, len(models), ", ".join(models[:6])))
                found.append((base, models))
            elif why.startswith("HTTP 401") or why.startswith("HTTP 403"):
                print("      %s -> needs an API key (%s)" % (base, why))
    if not found:
        print("\n  No local OpenAI-style server answered.")
        print("  AnythingLLM's built-in engine is private to AnythingLLM, so Cerry cannot use it directly.")
        print("  Fix: run the model in Ollama or LM Studio (or use AnythingLLM's developer API, port 3001,")
        print("  with a key from Settings > Developer API) and put that address in Cerry's local provider.")
    return found


def chat(base, model, key):
    print("\nStep 2: asking %s a short question (up to 5 minutes) ...\n" % model)
    headers = {"Authorization": "Bearer " + key} if key else {}
    body = {"model": model, "stream": True, "messages": [{"role": "user", "content": "Say hello in five words."}]}
    start, first, text = time.monotonic(), None, ""
    try:
        with httpx.stream("POST", base + "/chat/completions", json=body, headers=headers,
                          timeout=httpx.Timeout(300.0, connect=10.0)) as r:
            if r.status_code != 200:
                print("  HTTP %d: %s" % (r.status_code, r.read().decode("utf-8", "replace")[:300]))
                return
            for line in r.iter_lines():
                if not line.startswith("data:") or line.strip() == "data: [DONE]":
                    continue
                try:
                    delta = json.loads(line[5:])["choices"][0].get("delta", {})
                except (ValueError, KeyError, IndexError):
                    continue
                piece = delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning") or ""
                if piece and first is None:
                    first = time.monotonic() - start
                text += delta.get("content") or ""
    except httpx.HTTPError as e:
        print("  Failed after %.0f s: %s" % (time.monotonic() - start, type(e).__name__))
        return
    total = time.monotonic() - start
    if first is None:
        print("  The server answered but sent no text (%.0f s)." % total)
        return
    print("  First output after %.1f s, finished after %.1f s." % (first, total))
    print("  Answer: %s" % (text.strip()[:120] or "(only reasoning text)"))
    if first > 30:
        print("\n  Cerry's default first-response deadline is 30 s, so it gave up before the model spoke.")
        print("  Raise 'First response deadline' in Cerry's top bar to at least %d." % (int(first) + 30))
    else:
        print("\n  The model is fast enough. Check the exact error shown in Cerry's status line / Activity window.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base")
    ap.add_argument("--model")
    ap.add_argument("--key", default="")
    a = ap.parse_args()
    load_env()
    key = a.key or os.getenv("ANYTHINGLLM_API_KEY", "")
    if a.base and a.model:
        chat(a.base.rstrip("/"), a.model, key)
        return
    found = scan(key)
    if found and found[0][1]:
        chat(found[0][0], found[0][1][0], key)


if __name__ == "__main__":
    sys.exit(main())
