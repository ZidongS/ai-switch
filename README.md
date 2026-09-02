# ai-switch

## The problem it solves

When you use multiple coding agents and multiple model providers, changing providers separately in Claude Code and Codex is repetitive and error-prone. **ai-switch updates both agents in one command.**

Choose a provider profile once, then activate it with `ai-switch use NAME`. The built-in GLM and DeepSeek presets already contain the provider-specific files, endpoints, protocols, model catalogs, and Claude Code mappings recommended by their official documentation. You only need to enter your API key. Custom OpenAI-compatible providers are supported too.

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
ai-switch describe default "Default daily configuration"
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
