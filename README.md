# cherry_ai (Beta)

Cherry is a Windows desktop AI workspace. You can ask questions about your documents, talk through a shared screen, draft a cover letter for a saved job, or review code changes before applying them. For chat, choose a cloud provider or a local model served by Ollama.

Select a model beside Send at the bottom right of the message box. In the left sidebar, **Token control** and its **Output limit** appear below chat navigation when the token plugin is enabled. Open **Response settings** for Writing style. Click any tool section heading to expand or fold it.

## Project layout

- `ai_workspace_voice.py` starts the application.
- `core/` contains reusable engines, workspace logic and the plugin loader.
- `plugins/` contains optional Cherry plugins.
- `config/` contains safe example settings used for a first installation.
- `tools/` contains optional diagnostic and maintenance scripts.
- `docs/` contains the plugin catalog and the planned mobile/server boundary.
- `ai_workspace_data/` is created at runtime and stores private settings, plugin configuration, chats, documents, logs and voice models. Keep this folder local.

The runtime plugin list is `ai_workspace_data/plugins.json`. The installer creates it from `config/plugins.example.json` only when it does not already exist, so your current plugin choices are preserved. Other JSON files in `ai_workspace_data/` hold runtime or plugin-specific settings; plugins create their defaults as needed. They are local user configuration, not removed from the project.

See [the plugin catalog](docs/PLUGIN_CATALOG.md) for a feature and dependency map. Use **Backup / Restore...** in Cherry to export runtime data to a ZIP or restore it. Backups omit `key.env`; restore merges files and makes a safety copy first. The local `ai_workspace_data/` folder remains the source of truth.

## Install on Windows

1. Install Python 3.10–3.13. The installer creates a project-local `.venv`, installs `requirements.txt`, creates missing runtime settings from the examples, and downloads the default English and German Piper voices into `ai_workspace_data/voices/`.
2. Open PowerShell in this folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

3. Add API keys to the root `key.env`. It is ignored by Git. Local Ollama needs no API key. Cherry connects to `http://127.0.0.1:11434/v1`. Use the exact model tag from `ollama list` in Cherry’s Models settings.
4. Start Cherry using the command printed by the installer, or run:

   ```powershell
   .\.venv\Scripts\python.exe .\ai_workspace_voice.py
   ```

The local speech-to-text model is downloaded the first time you select local transcription. The default configuration uses Groq for transcription, so add `GROQ_API_KEY` if you want that option. **Live** connects directly to Google's Gemini Live API for streaming microphone input and generated audio; it requires `GEMINI_API_KEY`, internet access, and uses Google's API limits and billing. It is separate from the selected chat model and does not run Cherry's model routing, tools or per-turn document retrieval.

## API keys

`config/key.env.example` lists environment-variable names used by the included providers and optional integrations, with empty values. The installer copies it to the ignored root file `key.env` if that file does not exist. Fill only the services you use. Never commit the real key file. If a key was previously pushed, revoke and replace it.

Cloud model calls send submitted prompts to the selected provider. This can include document excerpts, a CV, job postings or attached files. Review provider and routing settings before sending sensitive material.

## Chat, voice, and screen sharing

Choose **Temporary chat** under **New chat** for a text conversation that stays out of Cherry’s local history and memory. You can ask follow-up questions until you close the temporary window. Cloud providers may retain requests. Select **Local only** to use a local model.

Click **Share screen** beside **Live** to share the primary display and microphone with Gemini Live. Ask a question about the visible content, such as “What would you ask me about this slide in a mock interview?” Cherry sends at most one JPEG frame per second and keeps the frames in memory. Spoken turns appear in the current chat history. **Stop sharing** ends both screen and microphone streaming. This requires `GEMINI_API_KEY`, internet access, and cloud routing.

In **Audio and voice → Quick talk**, start speaking during Cherry’s answer to interrupt playback and give your next instruction. Headphones help the microphone distinguish your voice from Cherry’s. For ordinary **Converse**, interruption follows the **Voice...** setting. In the main chat you can also type a follow-up during generation and press **Enter** or click **Interrupt & send**. Cherry stops the current reply and sends the follow-up in the same chat.

## Job search and cover letters

Open **Jobs** to search for roles that match your CV. Select a result and open **Cover letter → Draft cover letter**. You can name an experience to emphasize for that role. Cherry uses your CV and the saved posting to draft the letter, then shows the draft and its fact-check notes together. Review the claims before sending an application.

Choose **Prepare documents** when you want both a tailored CV and a letter. To shortlist roles first, leave **Also write tailored CV and cover letter** unchecked during the search.

## GitHub editing

To edit a GitHub file directly, open **Files and images → GitHub...** or enter `/github owner/repo` in chat. In **Add token...**, use a token with **Contents → Read and write** for that repository. Load the files, double-click a text file, then edit it yourself or enter an instruction and choose **Edit file with Cerry**. **New file...** lets you enter a repository path. Choose **Use loaded branch** to commit directly there, or enter a working branch name. For the default branch, also select **Allow commits to the default branch**. Select **Commit changes...**, enter a commit message, review the diff, and select **Commit to GitHub**. GitHub branch protection rules still apply.

## Workspace documents

Cherry keeps a local SQLite document memory in each workspace. Opening a workspace makes its saved index available and starts a background check for added, changed, and removed files. Searching this index uses no model tokens. Naming a file in a question narrows retrieval to that document.

In **Manage workspace → Documents**, select an included file and choose **Ask a question** to use passage search, or **Review whole document** to review every extractable section. A new review uses your configured model routing. Repeating the same instructions on the same indexed content reopens the saved review. After editing a file, let the background check finish or click **Re-scan**.

## Plugins

Plugin source is in `plugins/`. The loader reads the enabled plugin names from `ai_workspace_data/plugins.json`. Its starter list is `config/plugins.example.json`. To enable a plugin, add its module name to the runtime JSON. Additional plugin modules can also be placed in `plugins/`.

Plugins are optional. Review them before enabling advanced integrations. In particular, `cerry_run` executes generated Python or R code with your user permissions; `cerry_hermes` and `cerry_hermes_voice` can run agent tasks; `cerry_outreach` prepares batches of applications and sends CV/job content to the selected model provider. These are not enabled in the starter list.

The `cerry_local_agent` plugin works without Hermes. Open **Code agent...** or type `/work <task>`, choose a project directory, and ask Cherry to edit it. Choose Cherry's configured models, Codex (ChatGPT sign-in), or Claude Code (Claude sign-in). CLI choices receive a temporary copy of eligible project text files and run in read-only/plan mode; they cannot directly edit the chosen folder. Cherry shows a diff and only applies changes after you approve. Original files are copied into `ai_workspace_data/local_agent_backups/` before changes are applied. Source files are sent to the selected model service, so use Local only routing for private code. Codex CLI and Claude Code must be installed and signed in separately.

## Python packages

`requirements.txt` installs the main app, common document formats, local speech, image/document ingestion and job-search support. `requirements-optional.txt` lists packages used by specialist plugins and generated scripts. Install only what you need, for example:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-optional.txt
```

Some optional features also need external software or model access. OCR through `pytesseract` requires the Tesseract application. Playwright requires browser installation (`python -m playwright install`). Audio diarization has a separate, large dependency file (`requirements-audio-diarization.txt`) because it may require separate model access.

LinkedIn and Indeed search uses `python-jobspy`; review those services' current terms before enabling or using that search source.

## Development

Install development tools with `powershell -ExecutionPolicy Bypass -File .\tools\dev_setup.ps1`. Run the checks with `.\.venv\Scripts\python.exe -m unittest discover -s tests` and lint with `.\.venv\Scripts\ruff.exe check .`. Tests use temporary directories and do not read or modify your real Cherry data. See [the server architecture notes](docs/SERVER_ARCHITECTURE.md) for the future mobile web client direction.
