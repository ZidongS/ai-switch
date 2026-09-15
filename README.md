# ai-switch

## The problem it solves

When you use multiple coding agents and multiple model providers, changing providers separately in Claude Code and Codex is repetitive and error-prone. **ai-switch updates both agents in one command.**

Choose a provider profile once, then activate it with `ai-switch use NAME`. The built-in GLM and DeepSeek presets already contain the provider-specific files, endpoints, protocols, model catalogs, and Claude Code mappings recommended by their official documentation. You only need to enter your API key. Custom OpenAI-compatible providers are supported too.

A profile can offer several models (for example GLM 5.3 and GLM 5.3 Flash, or DeepSeek Flash and DeepSeek V4 Pro). `ai-switch use` asks which one to activate and writes the answer to both agents, so the model pickers *inside* Claude Code and Codex keep working instead of being pinned to a single model.

Switching also leaves conversation history alone: sessions, rollouts, `history.jsonl` and the agents' runtime databases are never copied into a profile or overwritten by a switch, and `ai-switch doctor` reports the things that really do make old sessions disappear.

This is a secure, headless-friendly command-line tool for servers without a desktop environment or administrator privileges.

## Install

Install from PyPI (recommended):

```bash
python3 -m pip install --user ai-switch-cli
```

For a system or virtual-environment install:

```bash
pip install ai-switch-cli
```

Install the latest development version directly from GitHub:

```bash
python3 -m pip install --user "git+https://github.com/ZidongS/ai-switch.git"
```

Or install a local checkout:

```bash
python3 -m pip install --user .
```

If `ai-switch` is not found afterwards, add the user script directory to `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Quick start

Save the configuration currently in use as a fallback profile:

```bash
ai-switch init default --description "Default daily configuration"
```

Create a ready-to-use mainstream provider profile interactively:

```bash
ai-switch add
```

Select `glm` or `deepseek`, enter the API key, and activate the generated configuration:

```bash
ai-switch use glm
```

`use` lists the models the profile offers and remembers your choice:

```text
This profile provides 2 models:
  1) glm-5.3         GLM-5.3 flagship (1M context)  [default]
  2) glm-5.3-flash   GLM-5.3 flash (fast, 1M context)
Select model [1-2, Enter=glm-5.3, q=cancel]:
```

For scripts and servers, choose without prompting (name, unique prefix, or index):

```bash
ai-switch use glm --model glm-5.3-flash
ai-switch use glm -m 2 -y
ai-switch use glm --dry-run          # show what would change
ai-switch use glm -m glm-5.3 --pin   # publish only this model (see below)
```

Selecting a model sets the **default for new sessions** in both agents, and the profile's other models stay in the catalogue so you can still switch inside the agent. That means an in-app `/model` choice is a per-session override — and Codex writes its own choice back to `~/.codex/config.toml` when it exits, so the two agents can drift apart without anything else noticing; `ai-switch doctor` reports exactly that. If you want an activation to be exclusive instead, add `--pin`: the published catalogue then holds only the selected model, so Codex's own picker has nothing else to switch to (the next `ai-switch use` without `--pin` restores the full menu).

Edit `~/.codex/config.toml` and `~/.claude/settings.json` for another provider, then save that configuration as a second profile:

```bash
ai-switch init glm --description "GLM Coding Plan"
ai-switch list
ai-switch use glm
ai-switch current
```

Create a profile through an interactive prompt (no editor required; API keys are hidden while typing). Choose the built-in `glm` preset to generate the complete ZAI Codex Responses configuration, Codex model catalog and Claude Code model/environment mappings for the three models the endpoint publishes (`glm-5.3`, `glm-5.3-flash`, `glm-5-turbo`) automatically. The `deepseek` preset creates the two model entries the DeepSeek endpoint currently accepts (`deepseek-flash` and `deepseek-v4-pro`, both with image input metadata) plus the recommended Claude Code mappings:

```bash
ai-switch add
```

Update an existing profile description:

```bash
ai-switch describe default "Default daily configuration"
```

`list` shows the active marker, profile name, configured clients, description, detected models, and endpoint hostnames. API keys are never printed.

## Choosing a model

Each profile keeps its model list in `models.json` next to the client files:

```bash
ai-switch models                 # list the models of the active profile
ai-switch models glm --json      # machine readable
ai-switch upgrade glm            # derive models.json for a profile from an older release
```

For every model a profile records the Codex catalogue entry and the Claude Code mapping, so activating one model updates all of these consistently:

* Codex: the top-level `model` (and `model_reasoning_effort`), plus a `~/.codex/models.json` catalogue that lists **every** model of the profile, which is what makes Codex's own `/model` picker show them.
* Claude Code: the default model in `settings.json` plus the Opus/Sonnet/Haiku mappings. The presets map the three categories to *different* provider models where they exist, and they never set `ANTHROPIC_MODEL`, because a pinned environment model overrides your selection and makes `/model` do nothing.

The `glm` and `deepseek` presets ship multi-model lists. A custom (OpenAI-compatible) profile is open-ended as well: when you create it you can name **several models at once, separated by spaces or commas** (for example `DeepSeek-V4.1-Flash Kimi-K3 Qwen3.8-Max`), and those become the profile's catalogue — both agents can then pick them. Any other name the endpoint serves still works with `ai-switch use NAME --model <name>`, and it is added to the catalogue when you use it.

### When a provider adds or renames a model

Model names change, and an endpoint refuses a name it does not know. Ask the provider what your key may use:

```bash
curl -s https://api.deepseek.com/models      -H "Authorization: Bearer $KEY"   # OpenAI-style list
curl -s https://open.bigmodel.cn/api/v1/models -H "Authorization: Bearer $KEY"  # Codex-format catalogue
```

A rejected name is also reported in the API error itself (*"The supported API model names are ..."*, or `模型不存在，请检查模型代码。`). Some providers publish their Codex catalogue directly (`/api/v1/models` on ZAI returns exactly the entries a profile's `codex-models.json` wants), so it can be copied verbatim. Historically DeepSeek answered to `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp`; today those are aliases that the endpoint resolves to `deepseek-flash`.

To add a model, append it to the profile's `models.json` — the `claude` mapping decides what Opus/Sonnet/Haiku resolve to, the `codex` entry controls whether Codex's own picker offers it. Keep the entries of names that older sessions were recorded with in the Codex catalogue so those sessions stay resumable, then activate:

```bash
ai-switch use deepseek -m deepseek-flash
```

Model suffixes are proxy-specific: DeepSeek's Claude endpoint accepts and strips a `[1m]` marker (`deepseek-v4-pro[1m]`), while ZAI rejects it as an unknown model — `glm-5.3` is already 1M context and must be used without a suffix. Worth a one-token request before trusting a name in a profile.

## Session history

Conversation history belongs to the agents, not to a provider, so ai-switch never manages it. These files are deliberately excluded from profiles and switches:

```text
~/.codex/history.jsonl, ~/.codex/session_index.jsonl, ~/.codex/sessions/,
~/.codex/*_N.sqlite (runtime state, logs, goals, memories)
~/.claude.json, ~/.claude/history.jsonl, ~/.claude/projects/, ~/.claude/sessions/
```

If sessions do seem to disappear after a switch, run:

```bash
ai-switch doctor          # full report; --json for tooling
ai-switch doctor --fix    # quarantine damaged Codex runtime databases
```

`doctor` checks, among other things:

* **Damaged SQLite runtime databases.** Codex keeps thread metadata in `~/.codex/state_*.sqlite` and `thread_history_1.sqlite`. When one is damaged (typical messages: `database disk image is malformed`, `file is not a database`) Codex silently stops listing recent sessions in `codex resume`. `doctor` detects this — reading a snapshot copy when the live file cannot be opened — and `--fix` moves the damaged files into `~/.config/ai-switch/quarantine/<timestamp>/`. Codex rebuilds the database from the rollout files on the next start, which brings resumable sessions back. Rollouts and history are not touched.
* **Network filesystems.** If `CODEX_HOME` lives on NFS/CIFS/SMB, SQLite databases are corrupted sooner or later, which is the usual root cause of "history lost after switching". Keep the runtime databases on a local disk instead:

  ```bash
  export CODEX_SQLITE_HOME=/var/tmp/codex-sqlite-$USER   # a local, persistent directory
  ```

  Codex only honours the **environment variable**: a `sqlite_home` key in `config.toml` is parsed but ignored (checked with `codex doctor`). `ai-switch add` asks for a local directory when it detects a network filesystem, creates it and prints the export line to put in your shell profile; `ai-switch doctor` reports the variable's status.
* **A stale model catalogue.** A catalogue left behind by the previous provider hides the new provider's models from Codex and makes sessions that used them unresumable. ai-switch removes that leftover file (only if it wrote it itself, and it backs it up first), and `doctor` verifies the configured model exists in the catalogue.
* **Disabled history persistence** (`[history] persistence = "none"`), a Claude Code model pinned by `ANTHROPIC_MODEL`, and `claude`/`codex` processes that are running while you switch and will rewrite their configuration on exit.

`codex resume` only lists sessions of the current directory by default; `codex resume --all` shows every directory. That cwd filter, not a lost session, is a common false alarm.

## Safety and storage

Before activation, the current Codex and Claude files are backed up under `~/.config/ai-switch/backups/<timestamp>/` (files that a switch removes are kept under `removed/`). Profiles are stored under `~/.config/ai-switch/profiles/`; directories use mode 700 and files use mode 600. The active profile and the last used model are recorded in `~/.config/ai-switch/state.json`. Restart `claude` or `codex` after switching so the process reloads its configuration.

Set `AI_SWITCH_HOME` to use a different profile directory, `CODEX_HOME`/`CLAUDE_CONFIG_DIR` for relocated agent configurations, and `AI_SWITCH_API_KEY` to fill the API key prompt non-interactively.

## Commands

```text
ai-switch init NAME [-d DESCRIPTION]  Save current files as a new profile
ai-switch list                        List profiles and configuration summaries
ai-switch use NAME [-m MODEL] [-y] [-n]  Back up and activate a profile (and a model)
ai-switch models [NAME] [--json]      Show the models a profile offers
ai-switch current                     Print the active profile
ai-switch describe NAME TEXT          Set a profile description
ai-switch upgrade NAME                Derive models.json for a profile from an older release
ai-switch add                         Create a profile interactively (GLM, DeepSeek, or custom)
ai-switch doctor [--fix] [--json]     Check configuration and session-history health
ai-switch --help                      Show full usage and examples
```

## Upgrading from 0.1

Existing profiles keep working. Profiles that only have `codex-models.json` are read as before and get a model picker automatically; run `ai-switch upgrade NAME` (or re-create the profile with `ai-switch add`) to materialise an editable `models.json` with explicit per-model Claude Code mappings.

## Development

```bash
python3 -m unittest -v
```

The project uses only the Python standard library.
