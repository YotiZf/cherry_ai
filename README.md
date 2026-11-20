# Cherry

Cherry is a Windows desktop AI workspace with chat, workspace documents, model routing, voice, job search and optional plugins. It supports cloud APIs and local endpoints such as AnythingLLM.

## Project layout

- `ai_workspace_voice.py` starts the application.
- `core/` contains reusable engines, workspace logic and the plugin loader.
- `plugins/` contains optional Cherry plugins.
- `config/` contains safe example settings used for a first installation.
- `tools/` contains optional diagnostic and maintenance scripts.
- `ai_workspace_data/` is created at runtime and stores private settings, plugin configuration, chats, documents, logs and voice models. Keep this folder local.

The runtime plugin list is `ai_workspace_data/plugins.json`. The installer creates it from `config/plugins.example.json` only when it does not already exist, so your current plugin choices are preserved. Other JSON files in `ai_workspace_data/` hold runtime or plugin-specific settings; plugins create their defaults as needed. They are local user configuration, not removed from the project.

## Install on Windows

1. Install Python 3.10–3.13. The installer creates a project-local `.venv`, installs `requirements.txt`, creates missing runtime settings from the examples, and downloads the default English and German Piper voices into `ai_workspace_data/voices/`.
2. Open PowerShell in this folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

3. Add API keys to the root `key.env`. It is ignored by Git. Local AnythingLLM models generally need no cloud API key; configure the endpoint and exact model IDs in Cherry's Models settings.
4. Start Cherry using the command printed by the installer, or run:

   ```powershell
   .\.venv\Scripts\python.exe .\ai_workspace_voice.py
   ```

The local speech-to-text model is downloaded the first time you select local transcription. The default configuration uses Groq for transcription, so add `GROQ_API_KEY` if you want that option.

## API keys

`config/key.env.example` lists environment-variable names used by the included providers and optional integrations, with empty values. The installer copies it to the ignored root file `key.env` if that file does not exist. Fill only the services you use. Never commit the real key file. If a key was previously pushed, revoke and replace it.

Cloud model calls send submitted prompts to the selected provider. This can include document excerpts, a CV, job postings or attached files. Review provider and routing settings before sending sensitive material.

## Plugins

Plugin source is in `plugins/`; the loader reads the enabled plugin names from `ai_workspace_data/plugins.json`. Its starter list is `config/plugins.example.json`. To enable a plugin, add its module name to the runtime JSON. Additional plugin modules can also be placed in `plugins/`.

Plugins are optional. Review them before enabling advanced integrations. In particular, `cerry_run` executes generated Python or R code with your user permissions; `cerry_hermes` and `cerry_hermes_voice` can run agent tasks; `cerry_outreach` prepares batches of applications and sends CV/job content to the selected model provider. These are not enabled in the starter list.

## Python packages

`requirements.txt` installs the main app, common document formats, local speech, image/document ingestion and job-search support. `requirements-optional.txt` lists packages used by specialist plugins and generated scripts. Install only what you need, for example:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-optional.txt
```

Some optional features also need external software or model access. OCR through `pytesseract` requires the Tesseract application. Playwright requires browser installation (`python -m playwright install`). Audio diarization has a separate, large dependency file (`requirements-audio-diarization.txt`) because it may require separate model access.

LinkedIn and Indeed search uses `python-jobspy`; review those services' current terms before enabling or using that search source.
