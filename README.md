# ai-switch

A small, secure command-line profile switcher for Codex and Claude Code. It works on headless servers and does not require administrator privileges.

## Install

```bash
chmod +x ai_switch.py
ln -sf "$PWD/ai_switch.py" "$HOME/.local/bin/ai-switch"
```

## Quick start

Save the configuration currently in use:

```bash
ai-switch init bairuo --description "Bairuo relay for daily work"
```

Edit `~/.codex/config.toml` and `~/.claude/settings.json` for another provider, then save that configuration as a second profile:

```bash
ai-switch init glm --description "GLM Coding Plan"
ai-switch list
ai-switch use glm
ai-switch current
```

Create a profile through an interactive prompt (no editor required; API keys are hidden while typing). Choose the built-in `glm` preset to generate the complete ZAI Codex Responses configuration, Codex model catalog, and Claude Code model/environment mappings automatically. The `deepseek` preset creates the three DeepSeek model entries, including image input metadata for `deepseek-v4-flash-vision-exp`, plus the recommended Claude Code mappings:

```bash
ai-switch add
```

Update an existing profile description:

```bash
ai-switch describe bairuo "Bairuo relay for daily work"
```

`list` shows the active marker, profile name, configured clients, description, detected models, and endpoint hostnames. API keys are never printed.

## Safety and storage

Before activation, the current Codex and Claude files are backed up under `~/.config/ai-switch/backups/`. Profiles are stored under `~/.config/ai-switch/profiles/`; directories use mode 700 and files use mode 600. Restart `claude` or `codex` after switching so the process reloads its configuration.

Set `AI_SWITCH_HOME` to use a different profile directory.

## Commands

```text
ai-switch init NAME [-d DESCRIPTION]  Save current files as a new profile
ai-switch list                         List profiles and configuration summaries
ai-switch use NAME                    Back up and activate a profile
ai-switch current                     Print the active profile
ai-switch describe NAME TEXT          Set a profile description
ai-switch add                          Create a profile interactively (GLM, DeepSeek, or custom)
ai-switch --help                      Show full usage and examples
```

## Development

```bash
python3 -m unittest -v
```

The project uses only the Python standard library.
