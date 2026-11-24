# Cherry server and phone client direction

Cherry's current interface is Tkinter. A mobile client should be a responsive web app/PWA, with the Python backend retaining ownership of provider keys and `ai_workspace_data/`.

## Suggested boundaries

- Chat API: create/read/update chats and stream assistant responses.
- Workspace API: list workspaces, manage notes and documents, request indexing.
- Model API: list configured models, test a provider/model on explicit user request, and report status without returning credentials.
- Authentication: require a user session/token for every endpoint; serve only over HTTPS and keep the service private (for example, behind Tailscale Serve).
- Persistence: one Cherry server owns the current workspace files. Do not synchronize a live data directory between multiple Cherry processes. Back up the directory instead.

Keep model/provider logic in Python so the Tkinter client and a future web client can call the same service functions. GUI-specific plugins need explicit web equivalents; they must not be assumed to work in a browser.

## Deployment choices

1. Windows laptop kept awake: run the Python service directly so it can reach Ollama on localhost.
2. Always-on home Linux host: Docker Compose can run the web service and mount a persistent data directory. Run the model service there too if local inference is needed.
3. Cloud host: use persistent storage and an authentication layer. Local models require suitable paid GPU capacity or an external model provider.

Do not expose an unauthenticated Cherry service to the public internet. A future deployment should have a backup/restore path before remote writes are enabled.
