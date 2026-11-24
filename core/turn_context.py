"""Settings and identity shared by a reply and its background work."""
from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnContext:
    workspace: object
    chat_id: str
    user_text: str
    document_source: str
    workspace_context: bool
    global_memory: bool
    workspace_memory: bool
    model: dict
    policy: object

    @classmethod
    def capture(cls, app, text):
        source = app.document_source_var.get()
        allow_memory = source != "Only use chat documents"
        workspace_context = allow_memory and app.use_ws.get()
        return cls(app.ws, app.chat["id"], text, source, workspace_context,
                   allow_memory and app.use_global.get(),
                   workspace_context and app.use_workspace_memory.get(),
                   app.selected_model(), deepcopy(app.policy))
