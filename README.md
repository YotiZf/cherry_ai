# Cherry

Cherry is a Windows desktop AI workspace with chat, workspace documents, model routing, voice, job search and optional plugins. The app can use OpenAI-compatible cloud APIs and local endpoints such as AnythingLLM.

## Install on Windows

1. Install Python 3.10–3.13. The current [Piper package metadata](https://pypi.org/project/piper-tts/) includes Python 3.13, while [python-jobspy](https://pypi.org/project/python-jobspy/) requires Python 3.10 or newer. This installer deliberately stops before Python 3.14 because local speech libraries can lag behind the newest Python releases.
2. Open PowerShell in this folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   The installer creates a project-local `.venv`, installs `requirements.txt`, creates private runtime settings from the example files when they are missing, and downloads the default English and German Piper voices into `ai_workspace_data/voices/`.
3. Add API keys to the root `key.env`. The file is ignored by Git. Local AnythingLLM models generally need no cloud API key; configure the endpoint and exact model IDs in Cherry’s Models settings.
4. Start Cherry using the command printed by the installer, or run:

   ```powershell
   .\.venv\Scripts\python.exe .\ai_workspace_voice.py
   ```

The local speech-to-text model is downloaded the first time you select local transcription. The default configuration uses Groq for transcription, so add `GROQ_API_KEY` if you want that option.

## API keys

`key.env.example` lists the environment-variable names used by the included providers and optional integrations, with empty values. The installer copies it to the ignored root file `key.env` if that file does not exist. Fill only the services you use. Never commit the real key file. If a key was previously pushed, revoke and replace it.

Cloud model calls send the prompts you submit to the selected provider. This can include document excerpts, a CV, job postings, or attached files. Review the provider and routing settings before sending sensitive material.

## Plugins

Plugin source files are in `plugins/`; reusable engines and the application entry point stay in the project root. The loader reads `ai_workspace_data/plugins.json`. The installer copies a starter selection from `plugins.example.json` if that runtime file does not exist.

Plugins are optional. Review the list before enabling advanced integrations. In particular, `cerry_run` executes generated Python or R code with your user permissions; `cerry_hermes` and `cerry_hermes_voice` can run agent tasks; `cerry_outreach` prepares batches of applications and sends CV/job content to the selected model provider. These are not enabled in the starter plugin list.

To enable one, add its module name to `ai_workspace_data/plugins.json`. The plugin loader also supports putting additional plugin modules in `plugins/`.

## Python packages

`requirements.txt` installs the main app, common document formats, local speech, image/document ingestion, and job-search support. `requirements-optional.txt` lists packages used by specialist plugins and generated scripts. Install only what you need, for example:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-optional.txt
```

Some optional features also need external software or model access. OCR through `pytesseract` requires the Tesseract application. Playwright requires browser installation (`python -m playwright install`). Audio diarization has a separate, large dependency file (`requirements-audio-diarization.txt`) because it may require separate model access.

LinkedIn and Indeed search uses `python-jobspy`; review those services' current terms before enabling or using that search source.

## Repository hygiene

`.gitignore` excludes `key.env`, the entire `ai_workspace_data/` folder, Python environments, downloaded model weights, generated documents and local databases. That data folder contains personal chats, documents, job-search material, logs and voice models; keep it private and out of commits. The example JSON and key files in the project root are safe starter templates.

No license is included. Add one only after deciding what reuse rights you want to grant.
