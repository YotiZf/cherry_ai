"""cerry_hermes_core: talk to the Hermes Agent API server and carry out one task through a chain of models.
No GUI in this file.  Hermes API used: GET /health, POST /v1/runs, GET /v1/runs/{id}, GET /v1/runs/{id}/events (SSE),
POST /v1/runs/{id}/stop, POST /v1/runs/{id}/approval, POST /v1/runs/{id}/clarify."""
import json
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
DEFAULTS = {
    "url": "http://127.0.0.1:8642",
    "api_key_env": "HERMES_API_KEY",          # name of the variable in key.env (optional, see find_key)
    "auto_start": True,                         # start "hermes gateway" yourself when it is not running
    "hermes_cmd": "hermes",
    "connect_timeout_s": 60,                    # cloud model: no sign of life within this time -> next model
    "local_connect_timeout_s": 300,             # local model: loading can take longer
    "total_timeout_s": 1800,                    # hard limit for one run
    "approval_wait_s": 280,                     # how long Cerry waits for your answer to a permission question
    "provider_slugs": {"gemini": "gemini", "groq": "groq", "openrouter": "openrouter", "nvidia": "nvidia"},
    "models_reference_file": "hermes_models_reference.json",
    "local_models": [{"label": "Local (Hermes llama.cpp)", "provider": "llamacpp", "model": ""}],
    "safe_roots": [],                           # folders Hermes may write to (only used when Cerry starts the gateway)
}


class HermesError(Exception):
    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal                      # fatal = trying another model cannot help


# ---------------------------------------------------------------- configuration
def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    path = Path(path)
    try:
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        try:
            path.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
        except OSError:
            pass
    cfg["_config_dir"] = str(path.resolve().parent)
    return cfg


def parse_env_text(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def hermes_home(env=None):
    env = os.environ if env is None else env
    if env.get("HERMES_HOME"):
        return Path(env["HERMES_HOME"])
    if os.name == "nt" and env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"]) / "hermes"
    return Path.home() / ".hermes"


def find_key(cfg, env=None, home=None):
    """HERMES_API_KEY from key.env, else API_SERVER_KEY from Hermes's own .env file."""
    env = os.environ if env is None else env
    if env.get(cfg.get("api_key_env", "HERMES_API_KEY")):
        return env[cfg.get("api_key_env", "HERMES_API_KEY")]
    home = Path(home) if home else hermes_home(env)
    try:
        return parse_env_text((home / ".env").read_text(encoding="utf-8")).get("API_SERVER_KEY", "")
    except OSError:
        return ""


# ---------------------------------------------------------------- model chain
def reference_models(models, cfg):
    """Read the reference each time routes are requested. Missing file uses Cerry's list;
    an existing malformed file raises an actionable error rather than hiding it.
    """
    filename = cfg.get("models_reference_file", "hermes_models_reference.json")
    if not filename:
        return list(models), "Cerry main model list"
    path = Path(filename).expanduser()
    if not path.is_absolute():
        path = Path(cfg.get("_config_dir", Path(__file__).resolve().parent / "ai_workspace_data")) / path
    if not path.exists():
        return list(models), "Cerry main model list (reference file missing: %s)" % path
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise HermesError("Cannot read model reference %s: %s" % (path, error), fatal=True)
    entries = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise HermesError("Model reference %s must contain a models array." % path, fatal=True)
    result, labels = [], set()
    for position, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise HermesError("Model reference entry %d must be an object." % position, fatal=True)
        if entry.get("enabled", True) is False:
            continue
        label, provider = entry.get("label"), entry.get("provider")
        model = entry.get("id", entry.get("model"))
        if not all(isinstance(v, str) and v.strip() for v in (label, provider, model)):
            raise HermesError("Model reference entry %d needs non-empty label, provider and id strings." % position, fatal=True)
        if label in labels:
            raise HermesError("Duplicate model label in reference: " + label, fatal=True)
        labels.add(label)
        result.append(dict(label=label, provider=provider, id=model))
    return result, str(path)


def build_routes(models, cfg):
    """Reference JSON (when present) replaces the cloud list; existing local_models remain separate."""
    models, origin = reference_models(models, cfg)
    slugs = dict(DEFAULTS["provider_slugs"])
    slugs.update(cfg.get("provider_slugs", {}))
    routes, seen, skipped = [], set(), []
    for m in models:
        slug = slugs.get(m.get("provider"))
        if slug and m["label"] not in seen:
            seen.add(m["label"])
            routes.append({"label": m["label"], "provider": slug, "model": m["id"], "local": False})
        else:
            skipped.append("%s: provider %s has no Hermes mapping" % (m["label"], m["provider"]))
    for m in cfg.get("local_models", []):
        if isinstance(m, dict) and m.get("label") and m.get("provider") and m["label"] not in seen:
            seen.add(m["label"])
            routes.append({"label": m["label"], "provider": m["provider"], "model": m.get("model", ""), "local": True})
    report = ["Model source: " + origin,
              "Configured routes (not connection-tested): %d" % len(routes)]
    report += ["%s | %s | %s | %s" % (r["label"], r["provider"], r["model"],
               "local fallback" if r["local"] else "cloud") for r in routes]
    report += ["SKIPPED: " + item for item in skipped]
    try:
        folder = Path(cfg.get("_config_dir", Path(__file__).resolve().parent / "ai_workspace_data"))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "hermes_models.log").write_text("\n".join(report) + "\n", encoding="utf-8")
    except OSError:
        pass
    return routes


def default_chain(routes, selected_label=None):
    cloud = [r for r in routes if not r["local"]]
    local = [r for r in routes if r["local"]]
    first = [r for r in cloud if r["label"] == selected_label]
    rest = [r for r in cloud if r["label"] != selected_label]
    return [r["label"] for r in first + rest + local]


def order_chain(labels, routes):
    """Keep the user's order, drop unknown names and always put local models last."""
    by = {r["label"]: r for r in routes}
    chain = [by[l] for l in labels if l in by]
    return [r for r in chain if not r["local"]] + [r for r in chain if r["local"]]


def build_task(task, folder=""):
    rules = ("You are carrying out a task for the user inside the Cerry desktop app. "
             + (f"Work only inside this folder and use absolute paths: {folder}. " if folder else "")
             + "Do not delete or overwrite existing files unless the task explicitly says so; create new files instead. "
             + "Ask a clarifying question only if the task cannot be done without it. "
             + "When you finish, reply with a short summary: what you did, the full paths of files you created or "
             + "changed, and anything the user should check.")
    return task.strip(), rules


def make_body(route, prompt, instructions, session_id):
    body = {"input": prompt, "instructions": instructions, "session_id": session_id, "provider": route["provider"]}
    if route.get("model"):
        body["model"] = route["model"]
    return body


# ---------------------------------------------------------------- server-sent events
def parse_sse(lines):
    """Yield (event_type, payload_dict) from an iterator of text lines.  Comment lines (keepalive) are skipped."""
    event, data = None, []

    def build():
        raw = "\n".join(data)
        if raw.strip() == "[DONE]":
            return "done", {}
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"text": raw}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        etype = event or payload.get("type") or payload.get("event") or ""
        return str(etype).lower(), payload

    for line in lines:
        if line == "":
            if event or data:
                yield build()
            event, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if event or data:
        yield build()


def classify(etype, payload):
    t = etype.lower()
    if t in ("run.completed", "completed"):
        return "completed"
    if t in ("run.failed", "failed", "error"):
        return "failed"
    if t in ("run.cancelled", "run.interrupted", "cancelled", "interrupted"):
        return "cancelled"
    if "approval" in t or "clarify" in t:
        return "control"
    if t.startswith("tool.started"):
        return "tool_start"
    if t.startswith("tool."):
        return "tool_done"
    if "delta" in t or "reasoning" in t or t.startswith("message") or t.startswith("subagent"):
        return "progress"
    return "other"


def pick_text(payload):
    for key in ("output", "result", "final_response", "response", "text", "content", "message"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


# ---------------------------------------------------------------- HTTP client
class Client:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.key = key

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.key}"}

    def _check(self, r, what):
        if r.status_code in (401, 403):
            raise HermesError(f"Hermes rejected the API key ({what}). Check API_SERVER_KEY / HERMES_API_KEY.", fatal=True)
        if r.status_code >= 400:
            raise HermesError(f"{what}: HTTP {r.status_code} {r.text[:200]}")
        return r

    def health(self):
        try:
            return httpx.get(self.url + "/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def capabilities(self):
        r = self._check(httpx.get(self.url + "/v1/capabilities", headers=self.headers, timeout=10), "capabilities")
        return r.json()

    def create_run(self, body):
        try:
            r = httpx.post(self.url + "/v1/runs", json=body, timeout=30,
                           headers=dict(self.headers, **{"Idempotency-Key": uuid.uuid4().hex}))
        except httpx.HTTPError as e:
            raise HermesError(f"Could not reach Hermes: {e}", fatal=True)
        run_id = self._check(r, "start run").json().get("run_id")
        if not run_id:
            raise HermesError("Hermes did not return a run id.")
        return run_id

    def status(self, run_id):
        return self._check(httpx.get(f"{self.url}/v1/runs/{run_id}", headers=self.headers, timeout=15), "status").json()

    def stop(self, run_id):
        return httpx.post(f"{self.url}/v1/runs/{run_id}/stop", headers=self.headers, timeout=15).status_code

    def post_control(self, run_id, kind, payload):
        return self._check(httpx.post(f"{self.url}/v1/runs/{run_id}/{kind}", json=payload, headers=self.headers,
                                      timeout=15), kind)

    def events(self, run_id):
        with httpx.stream("GET", f"{self.url}/v1/runs/{run_id}/events", headers=self.headers,
                          timeout=httpx.Timeout(10.0, read=None)) as r:
            if r.status_code >= 400:
                r.read()
                self._check(r, "events")
            yield from parse_sse(r.iter_lines())


# ---------------------------------------------------------------- one attempt with one model
class Ctx:
    """Callbacks from the caller: log(text), ask(control_dict, is_clarify) -> dict, stopped() -> bool."""

    def __init__(self, log, ask, stopped):
        self.log, self.ask, self.stopped = log, ask, stopped


def wait_terminal(client, run_id, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if client.status(run_id).get("status") in TERMINAL:
                return True
        except HermesError:
            return False
        time.sleep(0.5)
    return False


def run_once(client, route, prompt, instructions, limit, cfg, ctx):
    result = {"status": "failed", "error": "", "text": "", "tools": 0, "fatal": False, "run_id": ""}
    try:
        run_id = client.create_run(make_body(route, prompt, instructions, f"cerry-{uuid.uuid4().hex[:10]}"))
    except HermesError as e:
        result.update(error=str(e), fatal=e.fatal)
        return result
    result["run_id"] = run_id
    events, halt = queue.Queue(), threading.Event()

    def reader():
        try:
            for item in client.events(run_id):
                if halt.is_set():
                    break
                events.put(item)
            events.put(("__end__", {}))
        except Exception as e:
            events.put(("__error__", {"error": str(e)}))

    threading.Thread(target=reader, daemon=True).start()
    started, last_poll = time.monotonic(), 0.0
    established, deltas, answered, seen_types = False, [], set(), set()

    def stop_run(status, error=""):
        halt.set()
        try:
            client.stop(run_id)
        except Exception:
            pass
        wait_terminal(client, run_id)
        result.update(status=status, error=error)
        return result

    def finish(status, text="", error=""):
        halt.set()
        result.update(status=status, text=text or "".join(deltas).strip(), error=error)
        return result

    def handle_controls(status_body):
        nonlocal established
        for ctl in status_body.get("pending_controls") or []:
            cid = ctl.get("prompt_id") or ctl.get("request_id") or ctl.get("id")
            if not cid or cid in answered:
                continue
            answered.add(cid)
            established = True
            kind = str(ctl.get("type") or ctl.get("kind") or "").lower()
            is_clarify = "clarify" in kind or ("question" in ctl and "command" not in ctl and "choices" not in ctl)
            reply = ctx.ask(ctl, is_clarify) or {}
            generation = ctl.get("execution_generation", status_body.get("execution_generation"))
            try:
                if is_clarify:
                    client.post_control(run_id, "clarify", {"request_id": cid, "execution_generation": generation,
                                                            "answer": reply.get("answer", "")})
                else:
                    client.post_control(run_id, "approval", {"request_id": cid, "execution_generation": generation,
                                                             "choice": reply.get("choice", "deny")})
                if is_clarify:
                    ctx.log("Answered the question.")
                else:
                    ctx.log("Allowed once." if reply.get("choice") == "once" else "Denied.")
            except HermesError as e:
                ctx.log(f"Could not send your answer: {e}")

    def from_status(body):
        s = body.get("status")
        if s == "completed":
            return finish("completed", pick_text(body))
        if s == "failed":
            return finish("failed", error=str(body.get("error") or "the run failed"))
        if s in ("cancelled", "interrupted"):
            return finish("cancelled")
        return None

    while True:
        now = time.monotonic()
        if ctx.stopped():
            return stop_run("cancelled")
        if not established and now - started > limit:
            return stop_run("timeout", f"no sign of life from the model after {int(limit)} s")
        if now - started > cfg["total_timeout_s"]:
            return stop_run("failed", "time limit for one run reached")
        try:
            etype, payload = events.get(timeout=0.4)
        except queue.Empty:
            etype, payload = None, None
        if etype is None and now - last_poll >= 3:
            last_poll = now
            try:
                body = client.status(run_id)
            except HermesError as e:
                if e.fatal:
                    return finish("failed", error=str(e))
                body = {}
            done = from_status(body)
            if done:
                return done
            if body.get("status") == "waiting_for_approval" or body.get("pending_controls"):
                established = True
                handle_controls(body)
            continue
        if etype is None:
            continue
        if etype == "__error__":
            try:
                done = from_status(client.status(run_id))
            except HermesError:
                done = None
            return done or finish("failed", error="lost the connection to Hermes: " + payload.get("error", ""))
        if etype == "__end__":
            try:
                done = from_status(client.status(run_id))
            except HermesError:
                done = None
            return done or finish("failed", error="the event stream ended without a result")
        kind = classify(etype, payload)
        if kind == "completed":
            return finish("completed", pick_text(payload))
        if kind == "failed":
            return finish("failed", error=str(payload.get("error") or payload.get("message") or "the run failed"))
        if kind == "cancelled":
            return finish("cancelled")
        if kind == "control":
            established = True
            try:
                handle_controls(client.status(run_id))
            except HermesError as e:
                ctx.log(f"Could not read the permission request: {e}")
        elif kind == "tool_start":
            established = True
            result["tools"] += 1
            ctx.log(f"▶ {payload.get('tool', 'tool')}: {str(payload.get('preview', ''))[:140]}")
        elif kind == "tool_done":
            established = True
            ok = not payload.get("error")
            ctx.log(f"{'✓' if ok else '✗'} {payload.get('tool', 'tool')} "
                    f"({float(payload.get('duration') or 0):.1f}s) {str(payload.get('preview', ''))[:100]}")
        elif kind == "progress":
            established = True
            piece = payload.get("delta") or payload.get("text") or ""
            if isinstance(piece, str) and etype.endswith("delta") and "reasoning" not in etype:
                deltas.append(piece)
        elif etype not in seen_types and etype not in ("run.started", "run.queued", "done"):
            seen_types.add(etype)
            ctx.log(f"(Hermes event: {etype})")        # helps to spot event names this version does not know yet


# ---------------------------------------------------------------- the whole chain
def run_chain(client, task, folder, chain, cfg, ctx):
    """Try the models in order.  A model is abandoned only if it did nothing yet (no tool was started)."""
    prompt, rules = build_task(task, folder)
    attempts = []
    for number, route in enumerate(chain, 1):
        if ctx.stopped():
            return {"status": "cancelled", "model": route["label"], "text": "", "attempts": attempts, "tools": 0}
        limit = cfg["local_connect_timeout_s"] if route["local"] else cfg["connect_timeout_s"]
        ctx.log(f"[{number}/{len(chain)}] {route['label']} - waiting up to {int(limit)} s for the model to respond")
        out = run_once(client, route, prompt, rules, limit, cfg, ctx)
        out["model"] = route["label"]
        out["attempts"] = attempts
        if out["status"] in ("completed", "cancelled"):
            return out
        attempts.append(f"{route['label']}: {out['status']} ({out['error'] or 'no details'})")
        ctx.log(f"✗ {route['label']} did not work: {out['error'] or out['status']}")
        if out["fatal"]:
            return out
        if out["tools"] > 0:
            out["error"] += " - tools had already started, so Cerry will NOT switch model automatically (retry by hand)"
            return out
        if number < len(chain):
            ctx.log("Switching to the next model...")
    return {"status": "failed", "model": chain[-1]["label"] if chain else "", "text": "", "tools": 0,
            "error": "No model worked.\n" + "\n".join(attempts), "attempts": attempts, "fatal": False}


# ---------------------------------------------------------------- starting the gateway
def ensure_gateway(cfg, log, data_dir):
    """Return (client, process_or_None).  Starts 'hermes gateway' when needed and allowed."""
    key = find_key(cfg)
    client = Client(cfg["url"], key)
    if client.health():
        if not key:
            raise HermesError("Hermes is running, but Cerry has no API key. Put API_SERVER_KEY=... in Hermes's .env "
                              "(Cerry reads it from there) or HERMES_API_KEY=... in key.env.", fatal=True)
        return client, None
    if not cfg.get("auto_start", True):
        raise HermesError("Hermes is not running. Start it with:  hermes gateway   (API_SERVER_ENABLED=true and "
                          "API_SERVER_KEY must be set in Hermes's .env).", fatal=True)
    cmd = shutil.which(cfg.get("hermes_cmd", "hermes"))
    if not cmd:
        raise HermesError("The 'hermes' command was not found. Install Hermes first, or set hermes_cmd in hermes.json.",
                          fatal=True)
    key = key or secrets.token_urlsafe(24)
    client.key = key
    env = dict(os.environ, API_SERVER_ENABLED="true", API_SERVER_KEY=key)
    roots = [str(r) for r in cfg.get("safe_roots", []) if r]
    if roots:
        env["HERMES_WRITE_SAFE_ROOT"] = os.pathsep.join(roots + [str(hermes_home())])
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             if os.name == "nt" else 0)
    logfile = Path(data_dir) / "gateway.log"
    log("Starting the Hermes gateway ...")
    handle = open(logfile, "ab")
    proc = subprocess.Popen([cmd, "gateway"], env=env, stdin=subprocess.DEVNULL, stdout=handle,
                            stderr=subprocess.STDOUT, creationflags=flags)
    for _ in range(90):
        if client.health():
            log("Hermes gateway is ready.")
            return client, proc
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    tail = ""
    try:
        tail = logfile.read_text(encoding="utf-8", errors="ignore")[-400:]
    except OSError:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    raise HermesError("The Hermes gateway did not become ready. Log: " + str(logfile) + "\n" + tail, fatal=True)
