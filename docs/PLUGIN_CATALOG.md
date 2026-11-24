# Plugin catalog

The authoritative enable/disable list is the ignored local file `ai_workspace_data/plugins.json`. The safe seed is `config/plugins.example.json`. Dependencies marked optional are documented in `requirements-optional.txt` or `requirements-audio-diarization.txt`.

| Plugin | Purpose | Extra dependency / service |
|---|---|---|
| `cerry_audio` | Recording, transcription and speech output | `sounddevice`, `faster-whisper`, Piper; optional audio packages |
| `cerry_chat` | Chat behavior, rendering and commands | Core |
| `cerry_chat_memory` | Chat memory and context | Core |
| `cerry_contextmenu` | Text context menu and input sizing | Core |
| `cerry_deadline` | Response deadlines and cancellation | Core |
| `cerry_dock` | Groups legacy plugin toolbar rows. Built-in tools appear in the scrollable sidebar. | Core |
| `cerry_docs` | Document extraction/cache | SQLite; format packages in main requirements |
| `cerry_export` | Export chat content | Core |
| `cerry_files` | File attachment and handling | Core |
| `cerry_github` | GitHub issue/repository integration | `GITHUB_TOKEN` optional |
| `cerry_hermes` | Hermes agent integration | Hermes gateway |
| `cerry_hermes_models` | Hermes model discovery | Hermes gateway |
| `cerry_hermes_voice` | Hermes voice task interface | Hermes gateway; voice dependencies |
| `cerry_images` | Image generation | Gemini or Cloudflare credentials |
| `cerry_ingest` | Document, image and audio ingestion | OCR/PDF packages; optional model access |
| `cerry_jobs_1` | Job search, matching and application drafting | `python-jobspy`; optional search providers |
| `cerry_jobs_latex` | CV/letter formatting and LaTeX output | LaTeX installation for PDF generation |
| `cerry_layout` | Main window layout | Core |
| `cerry_links` | Link and repository actions | GitHub token for private repositories |
| `cerry_memory` | Long-term memory tools | Core |
| `cerry_models` | Manage/test configured models and providers | Configured provider keys |
| `cerry_outreach` | Application outreach workflow | Selected model provider; review before enabling |
| `cerry_pdf_study` | PDF study helpers | PDF packages |
| `cerry_restart` | Restart controls | Core |
| `cerry_routing` | Routing and fallback controls | Core |
| `cerry_run` | Run generated Python/R scripts | Python/R; executes with the user's permissions |
| `cerry_style` | Style and response controls | Core |
| `cerry_style_depth` | Style depth controls | Core |
| `cerry_textsize` | Text sizing controls | Core |
| `cerry_tokens` | Token usage display | Provider response metadata |
| `cerry_tuning` | Model tuning controls | Core |
| `cerry_unlink` | Workspace file unlink actions | Core |
| `cerry_vision` | Image understanding | Vision-capable model/provider |
| `cerry_web_reader` | Web search and page reading | Optional `ddgs` or `tavily-python` |

Review a plugin's source and permissions before enabling it. Optional integrations may require credentials and may send submitted text to third-party services.
