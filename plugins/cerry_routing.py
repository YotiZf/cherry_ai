"""Cerry routing guard: new providers take part in the fallback chain.

Problem it fixes: routing.json lists the providers that may be used as fallback (gemini, groq, openrouter,
anythingllm). A provider added later - for example NVIDIA through the model manager - was never in that list,
so its models were skipped whenever Cerry fell back to another model.
Now every provider that appears in PROVIDERS and that Cerry has not seen before is added to the allowed list once
(and routing.json is saved). Providers you untick in "Routing..." afterwards stay unticked.
Switch on:  "cerry_routing" in ai_workspace_data/plugins.json
"""
import json

ORDER = 20
ORIGINAL_PROVIDERS = {"gemini", "groq", "openrouter", "anythingllm"}     # decisions about these are never overridden


def install(ns):
    routing = ns["routing"]
    if getattr(routing, "_cr_installed", False):
        return
    routing._cr_installed = True
    path = ns["DATA_DIR"] / "routing_known.json"
    try:
        known = set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        known = set(ORIGINAL_PROVIDERS)
    original = routing.build_order

    def sync(policy):
        new = [p for p in ns["PROVIDERS"] if p not in known]
        if not new:
            return
        for name in new:
            known.add(name)
            if name not in policy.allowed_providers:
                policy.allowed_providers.append(name)
        try:
            policy.save(ns["ROUTING_PATH"])
            path.write_text(json.dumps(sorted(known)), encoding="utf-8")
        except OSError:
            pass

    def build_order(models, selected, policy):
        try:
            sync(policy)
        except Exception:
            pass
        return original(models, selected, policy)

    routing.build_order = build_order
