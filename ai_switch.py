#!/usr/bin/env python3
"""ai-switch: one command to move Claude Code and Codex between model vendors.

Model choice at activation time
    A profile can describe several models (``models.json``).  ``ai-switch use``
    lets you pick one and writes the choice to both agents: Codex gets the top
    level ``model`` plus the whole model catalogue, Claude Code gets its default
    model plus the Opus/Sonnet/Haiku mappings.  Model pickers inside the agents
    keep working instead of being pinned to one model.

        ai-switch use NAME                 pick a model (Enter accepts the default)
        ai-switch use NAME --model glm-5.3 activate one model without prompting
        ai-switch models NAME              show the models a profile offers

Conversation history is never part of a profile
    Sessions, rollouts, ``history.jsonl`` and the runtime SQLite databases are
    owned by the agents.  ai-switch keeps them out of profiles and switches,
    removes the stale model catalogue the previous provider left behind, and
    ``ai-switch doctor`` reports the real reasons sessions disappear after a
    switch (corrupt runtime SQLite databases, SQLite stored on NFS, disabled
    history persistence, claude/codex running while switching).

Gateways that cannot follow Codex's tool calls
    A gateway that translates the Responses API into Chat completions may reject
    the follow-up request of every tool-using turn, because Codex writes an
    assistant message item between the turn's tool calls and their results.  For
    such a profile (see GATEWAY_PATCHES) ``ai-switch use`` starts a local proxy
    that moves that item back in front of the calls, points Codex at it and stops
    it again on the next switch; ``doctor`` reports it and ``current`` shows it.
"""
import argparse, hashlib, json, os, re, shutil, signal, socket, sqlite3, subprocess, sys, tempfile, \
    textwrap, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.5.0"

# set by --no-color; Style() consults it so every command honours one switch
COLOR_DISABLED = False


def _env_path(name, default):
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


HOME = Path.home()
CODEX_DIR = _env_path("CODEX_HOME", HOME / ".codex")
CLAUDE_DIR = _env_path("CLAUDE_CONFIG_DIR", HOME / ".claude")
CODEX = CODEX_DIR / "config.toml"
CLAUDE = CLAUDE_DIR / "settings.json"
CODEX_AUTH = CODEX_DIR / "auth.json"
CODEX_MODELS = CODEX_DIR / "models.json"
ROOT = _env_path("AI_SWITCH_HOME", HOME / ".config" / "ai-switch")
PROFILES = ROOT / "profiles"
CURRENT = ROOT / "current"
STATE = ROOT / "state.json"
QUARANTINE = ROOT / "quarantine"

# Files a profile may own.  Session/history/rollout/runtime-DB files are never
# listed here on purpose: they belong to the agents, not to a provider profile.
PROFILE_FILES = ("codex-config.toml", "claude-settings.json", "codex-auth.json", "codex-models.json")

# Claude Code environment variables that describe *which provider/model* is used.
# They are replaced on every switch so the previous provider cannot leak into the
# next one; everything else in ``env`` is left untouched.
CLAUDE_STRUCTURAL_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                         "ANTHROPIC_CUSTOM_HEADERS", "API_TIMEOUT_MS",
                         "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                         "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_EFFORT_LEVEL",
                         "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "ANTHROPIC_EXTRA_BETAS")
CLAUDE_MODEL_PINS = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                     "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL_ALIASES")
CLAUDE_PROVIDER_ENV = CLAUDE_STRUCTURAL_ENV + CLAUDE_MODEL_PINS + ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")

GLM_ENDPOINT = "https://open.bigmodel.cn/api/v1"
GLM_CLAUDE_ENDPOINT = "https://open.bigmodel.cn/api/anthropic"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/"
DEEPSEEK_CLAUDE_ENDPOINT = "https://api.deepseek.com/anthropic"

NETWORK_FS = ("nfs", "nfs4", "cifs", "smb", "smb2", "smb3", "isilon", "lustre", "gpfs", "beegfs",
              "glusterfs", "ceph", "9p", "afs", "davfs", "fuse.sshfs", "fuse.s3fs", "ncpfs", "ocfs2")


# ---------------------------------------------------------------- small helpers
def secure(path):
    try:
        path.chmod(0o700 if path.is_dir() else 0o600)
    except OSError:
        pass


def write_atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    secure(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_text(path):
    return path.read_text(errors="replace") if path.exists() else None


def digest(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def display_path(path):
    try:
        return "~/" + str(Path(path).relative_to(HOME))
    except ValueError:
        return str(path)


def expand(path):
    """Expand ``~`` against this tool's HOME instead of the process environment."""
    text = str(path)
    if text == "~":
        return HOME
    if text.startswith("~/"):
        return HOME / text[2:]
    return Path(text)


def timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def blank_profile_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    secure(path)


# ---------------------------------------------------------------- presentation
class Style:
    """ANSI helpers that degrade to plain text (pipes, NO_COLOR=1, dumb terminals)."""

    CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
             "magenta": "35", "cyan": "36", "grey": "90"}

    def __init__(self, stream=None, force_off=False):
        stream = sys.stdout if stream is None else stream
        self.terminal = bool(getattr(stream, "isatty", lambda: False)())
        self.on = (self.terminal and not (force_off or COLOR_DISABLED)
                   and not os.environ.get("NO_COLOR") and os.environ.get("TERM", "") != "dumb")

    def __call__(self, text, *names):
        if not self.on or not names:
            return str(text)
        codes = ";".join(self.CODES[name] for name in names if name in self.CODES)
        return f"\x1b[{codes}m{text}\x1b[0m" if codes else str(text)

    def rule(self, width=68):
        return self("─" * width, "grey")


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR_WIDTH = 16
BAR_FULL, BAR_EMPTY = "▰", "▱"
# 256-colour ramp for the bar: cyan → teal → green as it fills
BAR_RAMP = (45, 44, 43, 49, 48, 47, 46, 82, 118)
BAR_HEAD, BAR_TRACK = 231, 238


class Progress:
    """Staged progress: spinner while each action runs, a bar, and a closing card.

    Animation is used on a terminal only; everywhere else the caller keeps the
    plain one-line-per-action output, so scripts and tests stay unaffected.
    """

    def __init__(self, total, style, stream=None, animate=True):
        self.style = style
        self.stream = stream or sys.stdout
        self.total = max(int(total), 1)
        self.done = 0
        # NO_COLOR mutes the colours, not the animation: a terminal still gets the
        # spinner and the bar (plain text when colour is off), everything else
        # keeps the one-line-per-action output.
        self.enabled = bool(style.terminal and animate
                            and not COLOR_DISABLED and os.environ.get("TERM", "") != "dumb")
        self.started = time.time()

    def _paint(self, text, colour):
        return f"\x1b[38;5;{colour}m{text}\x1b[0m" if self.style.on else text

    def _bar(self, done=None, width=BAR_WIDTH):
        done = self.done if done is None else done
        filled = max(0, min(width, round(width * done / max(self.total, 1))))
        cells = []
        for index in range(width):
            if index >= filled:
                cells.append(self._paint(BAR_EMPTY, BAR_TRACK))
            elif index == filled - 1:
                cells.append(self._paint(BAR_FULL, BAR_HEAD))          # leading edge
            else:
                ramp = BAR_RAMP[min(len(BAR_RAMP) - 1, index * len(BAR_RAMP) // width)]
                cells.append(self._paint(BAR_FULL, ramp))
        return "".join(cells)

    def _line(self, head, text, detail="", done=None):
        done = self.done if done is None else done
        percent = min(100, round(100 * done / max(self.total, 1)))
        counter = self.style(f"{int(round(done))}/{self.total}", "grey")
        tail = f"  {self.style(detail, 'grey')}" if detail else ""
        return (f"\r\x1b[K  {head} {self._bar(done)} {self.style(f'{percent:>3}%', 'green')} "
                f"{counter}  {text}{tail}")

    def animate(self, text, frames=6, delay=0.03):
        if not self.enabled:
            return
        for index in range(frames):
            self.stream.write(self._line(self.style(SPINNER[index % len(SPINNER)], "cyan"), text))
            self.stream.flush()
            time.sleep(delay)

    def step(self, text, detail=""):
        if not self.enabled:
            self.stream.write(text + (f": {detail}" if detail else "") + "\n")
            self.stream.flush()
            self.done += 1
            return
        self.done += 1
        self.stream.write(self._line(self.style("✓", "green"), text, detail) + "\n")
        self.stream.flush()

    def sweep(self, text="all set", frames=10, delay=0.025):
        """Fill the bar from zero to full, so it always finishes at 100%."""
        if not self.enabled:
            return
        for index in range(frames + 1):
            filled = round(BAR_WIDTH * index / frames)
            self.stream.write(self._line(self.style(SPINNER[index % len(SPINNER)], "cyan"),
                                         text, done=filled * self.total / BAR_WIDTH))
            self.stream.flush()
            time.sleep(delay)
        self.done = self.total
        self.stream.write(f"\r\x1b[K  {self.style('✓', 'green')} {self._bar()} "
                          f"{self.style('100%', 'green')} {self.style(f'{self.total}/{self.total}', 'grey')}"
                          f"  {text}\n")
        self.stream.flush()

    def result(self, title, rows):
        elapsed = time.time() - self.started
        if not self.enabled:
            print(title)
            for row in rows:
                label, value = row[0], row[1]
                print(f"{label}: {value}" if label else value)
            return
        entries = []
        for row in rows:
            label, plain = row[0], row[1]
            styled = row[2] if len(row) > 2 and row[2] else plain
            entries.append((label, plain, styled))
        head, stamp = f"✦ {title}", f"({elapsed:.2f}s)"
        # content width between the two border characters: one space of padding on
        # each side, an 8-character label column, then the value
        inner = max(len(head) + 2 + len(stamp), 8 + max(len(p) for _, p, _ in entries) + 2)
        top = "╭" + "─" * inner + "╮"
        self.stream.write("\n" + self.style(top, "grey") + "\n")
        self.stream.write(self.style("│", "grey") + " "
                          + self.style(head, "bold", "green")
                          + " " * (inner - 2 - len(head) - len(stamp))
                          + self.style(stamp, "grey") + " " + self.style("│", "grey") + "\n")
        self.stream.write(self.style("├" + "─" * inner + "┤", "grey") + "\n")
        for label, plain, styled in entries:
            pad = " " * max(0, inner - 10 - len(plain))
            self.stream.write(self.style("│", "grey") + " " + self.style(f"{label:<8}", "grey")
                              + styled + pad + " " + self.style("│", "grey") + "\n")
        self.stream.write(self.style("╰" + "─" * inner + "╯", "grey") + "\n")
        self.stream.flush()


# ---------------------------------------------------------------- profile layout
def targets():
    """Live files a profile can control, keyed by their name inside the profile."""
    return {"codex-config.toml": CODEX, "claude-settings.json": CLAUDE,
            "codex-auth.json": CODEX_AUTH, "codex-models.json": CODEX_MODELS}


def history_paths():
    """Agent-owned state that must never be copied into or out of a profile."""
    paths = [CODEX_DIR / "history.jsonl", CODEX_DIR / "session_index.jsonl",
             CODEX_DIR / "sessions", CODEX_DIR / "archived_sessions",
             CODEX_DIR / "thread_history_1.sqlite", CODEX_DIR / "version.json",
             CLAUDE_DIR / "history.jsonl", CLAUDE_DIR / "projects", CLAUDE_DIR / "sessions",
             CLAUDE_DIR / "session-env", CLAUDE_DIR / "file-history", CLAUDE_DIR / "todos",
             HOME / ".claude.json"]
    for pattern in ("state_*.sqlite", "logs_*.sqlite", "memories_*.sqlite", "goals_*.sqlite", "queue_*.sqlite",
                    "state_*.sqlite-wal", "state_*.sqlite-shm", "logs_*.sqlite-wal", "logs_*.sqlite-shm"):
        paths.extend(sorted(CODEX_DIR.glob(pattern)))
    return paths


def is_history_path(path):
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return False
    for owned in history_paths():
        try:
            owned_resolved = owned.resolve()
        except OSError:
            continue
        if resolved == owned_resolved or owned_resolved in resolved.parents:
            return True
    return False


def profile(name):
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("invalid profile name")
    return PROFILES / name


def load_state():
    try:
        state = json.loads(STATE.read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    write_atomic(STATE, json.dumps(state, indent=2) + "\n")


def active_profile():
    return CURRENT.read_text().strip() if CURRENT.exists() else ""


def last_model(name):
    models = load_state().get("models")
    return models.get(name) if isinstance(models, dict) else None


def profile_meta(d):
    try:
        meta = json.loads((d / "profile.json").read_text())
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- TOML fragments
def split_toml(text):
    """Split a TOML document into (top-level keys, sections)."""
    match = re.search(r"(?m)^\[", text)
    return (text, "") if not match else (text[:match.start()], text[match.start():])


def top_level_get(text, key):
    head, _ = split_toml(text)
    match = re.search(r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*(.+?)[ \t]*$", head)
    if not match:
        return None
    raw = match.group(1).strip()
    if raw[:1] in ("'", '"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    return raw


def top_level_set(text, key, value, create=True):
    head, tail = split_toml(text)
    line = f"{key} = {json.dumps(value)}"
    match = re.compile(r"(?m)^([ \t]*)" + re.escape(key) + r"[ \t]*=[^\n]*$")
    if match.search(head):
        head = match.sub(lambda _: line, head, count=1)
    elif create:
        if head and not head.endswith("\n"):
            head += "\n"
        head += line + "\n"
    return head + tail


def top_level_drop(text, key):
    head, tail = split_toml(text)
    head = re.sub(r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=[^\n]*\n?", "", head)
    return head + tail


def section_value(text, section, key):
    _, tail = split_toml(text)
    match = re.search(r"(?ms)^\[" + re.escape(section) + r"\][ \t]*\n(.*?)(?=^\[|\Z)", tail)
    if not match:
        return None
    found = re.search(r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*(.+?)[ \t]*$", match.group(1))
    return found.group(1).strip().strip("'\"") if found else None


# ---------------------------------------------------------------- Claude settings
def build_claude_settings(live, profile_settings, entry):
    """Merge a profile's Claude settings into the live file.

    Provider keys are owned by the profile and always replaced, so a switch can
    never leave the previous endpoint, token or model behind.  Keys the agent
    itself manages (``modelSettings``, ``availableModels``, onboarding flags,
    ...) are kept from the live file so a switch does not reset the agent state.
    """
    profile_settings = profile_settings if isinstance(profile_settings, dict) else {}
    out = {}
    if isinstance(live, dict):
        out.update({k: v for k, v in live.items() if k not in ("env", "model")})
    out.update({k: v for k, v in profile_settings.items() if k != "env"})
    live_env = live.get("env") if isinstance(live, dict) and isinstance(live.get("env"), dict) else {}
    prof_env = profile_settings.get("env") if isinstance(profile_settings.get("env"), dict) else {}
    env = {k: v for k, v in live_env.items() if k not in CLAUDE_PROVIDER_ENV}
    env.update({k: v for k, v in prof_env.items() if k not in CLAUDE_MODEL_PINS})
    if entry:
        mapped = entry.get("claude") if isinstance(entry.get("claude"), dict) else {}
        env.update(mapped.get("env") or {})
        out["model"] = mapped.get("model") or entry["slug"]
    elif "model" in profile_settings:
        out["model"] = profile_settings["model"]
    out["env"] = env
    return out


def substitute_model(value, old_slug, new_slug):
    """``glm-5.3[1m]`` -> ``glm-5.3-flash[1m]`` when the base name matches."""
    if not isinstance(value, str):
        return value
    base, _, suffix = value.partition("[")
    return new_slug + ("[" + suffix if suffix else "") if base == old_slug else value


def derive_claude_mapping(settings, default_slug, slug):
    """Best-effort per-model Claude mapping for profiles that predate models.json."""
    settings = settings if isinstance(settings, dict) else {}
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    pins = {k: substitute_model(v, default_slug, slug) for k, v in env.items() if k in CLAUDE_MODEL_PINS}
    pins.pop("ANTHROPIC_MODEL", None)  # a pinned main model blocks /model category switching
    model = substitute_model(settings.get("model") or default_slug, default_slug, slug)
    return {"model": model, "env": pins}


# ---------------------------------------------------------------- model catalogue
def codex_entry(slug, description, modalities, priority, context, effort):
    return {"slug": slug, "display_name": slug, "description": description,
            "default_reasoning_level": effort,
            "supported_reasoning_levels": [{"effort": "low", "description": "Light reasoning"},
                                            {"effort": "high", "description": "Enhanced reasoning"},
                                            {"effort": "max", "description": "Deep reasoning"}],
            "shell_type": "shell_command", "visibility": "list", "supported_in_api": True,
            "priority": priority, "base_instructions": "", "supports_reasoning_summaries": True,
            "default_reasoning_summary": "none", "support_verbosity": False,
            "apply_patch_tool_type": "freeform", "truncation_policy": {"mode": "bytes", "limit": 10000},
            "context_window": context, "max_context_window": context,
            "effective_context_window_percent": 95, "supports_parallel_tool_calls": True,
            "experimental_supported_tools": [], "input_modalities": list(modalities)}


def claude_mapping(model, opus=None, sonnet=None, haiku=None, subagent=None):
    env = {}
    if opus:
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = opus
    if sonnet:
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = sonnet
    if haiku:
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku
    if subagent:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = subagent
    return {"model": model, "env": env}


def adhoc_model(slug, index=99):
    """Model entry for an endpoint whose catalogue we do not know (custom provider).

    Every Claude Code category points at the same model: a gateway usually serves
    one namespace, so Claude's own alias names (``claude-sonnet-*``) would be
    rejected by it.
    """
    return {"slug": slug, "label": "custom model", "reasoning": "",
            "codex": codex_entry(slug, "custom model", ("text",), index, 1048576, "high"),
            "claude": claude_mapping(slug, opus=slug, sonnet=slug, haiku=slug, subagent=slug)}


def split_models(text):
    return [part for part in re.split(r"[,\s]+", text or "") if part]


GLM_MODELS = [
    # Model names are exactly what the ZAI endpoint accepts; the ``[1m]`` marker
    # some proxies use is rejected there (glm-5.3 is 1M context by default), so
    # Claude Code gets the plain names.
    {"slug": "glm-5.3", "label": "GLM-5.3 flagship (1M context, text)", "reasoning": "max",
     "codex": codex_entry("glm-5.3", "Z.ai's latest flagship model", ("text",), 0, 1048576, "max"),
     "claude": claude_mapping("glm-5.3", opus="glm-5.3", sonnet="glm-5.3", haiku="glm-5.3-flash",
                              subagent="glm-5.3-flash")},
    {"slug": "glm-5.3-flash", "label": "GLM-5.3 Flash (fast, text+image, 1M context)", "reasoning": "max",
     "codex": codex_entry("glm-5.3-flash", "Fast multimodal coding model", ("text", "image"), 1, 1048576, "max"),
     "claude": claude_mapping("glm-5.3-flash", haiku="glm-5.3-flash", subagent="glm-5.3-flash")},
    {"slug": "glm-5-turbo", "label": "GLM-5 Turbo (agent-optimized, 200K context)", "reasoning": "max",
     "codex": codex_entry("glm-5-turbo", "Agent-optimized model", ("text",), 2, 204800, "max"),
     "claude": {"model": "glm-5-turbo",
                "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5-turbo",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5-turbo",
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5-turbo",
                        "CLAUDE_CODE_SUBAGENT_MODEL": "glm-5-turbo",
                        # 200K context, so auto-compaction must happen well before 1M.
                        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "190000"}}},
]
DEEPSEEK_MODELS = [
    {"slug": "deepseek-flash", "label": "DeepSeek Flash (fast, current generation, text+image)", "reasoning": "high",
     "codex": codex_entry("deepseek-flash", "Fast general-purpose DeepSeek model", ("text", "image"), 0, 1048576, "high"),
     "claude": claude_mapping("deepseek-flash[1m]", opus="deepseek-flash[1m]", sonnet="deepseek-flash[1m]",
                              haiku="deepseek-flash", subagent="deepseek-flash")},
    {"slug": "deepseek-v4-pro", "label": "DeepSeek V4 Pro (deep reasoning, text+image)", "reasoning": "max",
     "codex": codex_entry("deepseek-v4-pro", "Deep reasoning DeepSeek model", ("text", "image"), 1, 1048576, "high"),
     "claude": claude_mapping("deepseek-v4-pro[1m]", opus="deepseek-v4-pro[1m]", sonnet="deepseek-v4-pro[1m]",
                              haiku="deepseek-flash", subagent="deepseek-flash")},
]

PRESETS = {
    "glm": {"endpoint": GLM_ENDPOINT, "claude_endpoint": GLM_CLAUDE_ENDPOINT,
            "common_env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
                           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": 1,
                           "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "API_TIMEOUT_MS": "3000000"},
            "models": GLM_MODELS, "default": "glm-5.3",
            "codex_template": ('model_provider = "ZAI"\nmodel = "@MODEL@"\nmodel_reasoning_effort = "@EFFORT@"\n'
                               'model_catalog_json = "@CATALOG@"\n\n[model_providers.ZAI]\nname = "ZAI"\n'
                               'base_url = "@ENDPOINT@"\nexperimental_bearer_token = "@KEY@"\n'
                               'wire_api = "responses"\n')},
    "deepseek": {"endpoint": DEEPSEEK_ENDPOINT, "claude_endpoint": DEEPSEEK_CLAUDE_ENDPOINT,
                 "common_env": {"CLAUDE_CODE_EFFORT_LEVEL": "max", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432",
                                "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"},
                 "models": DEEPSEEK_MODELS, "default": "deepseek-flash",
                 "codex_template": ('model = "@MODEL@"\nmodel_provider = "deepseek"\npreferred_auth_method = "apikey"\n'
                                    'forced_login_method = "api"\nmodel_reasoning_effort = "@EFFORT@"\n'
                                    'model_catalog_json = "@CATALOG@"\n\n[model_providers.deepseek]\nname = "deepseek"\n'
                                    'base_url = "@ENDPOINT@"\nwire_api = "responses"\n'
                                    'experimental_bearer_token = "@KEY@"\n')},
}


def normalise_catalog(raw, profile_settings=None, provider=None):
    """Turn any profile-side description of models into a catalogue dict."""
    if not isinstance(raw, dict):
        return None
    models = [m for m in raw.get("models") or [] if isinstance(m, dict) and m.get("slug")]
    if not models:
        return None
    default = raw.get("default") or models[0]["slug"]
    return {"version": 2, "provider": raw.get("provider") or provider or "", "default": default,
            "models": models, "settings": profile_settings, "open_ended": bool(raw.get("open_ended"))}


def load_catalog(d, profile_settings=None):
    """Return the selectable models of a profile, or None for legacy profiles.

    ``models.json`` is authoritative.  ``codex-models.json`` (written by older
    releases) is accepted and upgraded in memory so existing profiles get the
    picker without being rewritten.
    """
    if profile_settings is None:
        profile_settings = parse_json_file(d / "claude-settings.json")
    path = d / "models.json"
    if path.exists():
        try:
            catalog = normalise_catalog(json.loads(path.read_text()), profile_settings)
            if catalog:
                return catalog
        except ValueError:
            pass
    catalog_path = d / "codex-models.json"
    if not catalog_path.exists():
        return None
    try:
        raw = json.loads(catalog_path.read_text())
    except ValueError:
        return None
    entries = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return None
    settings = profile_settings if isinstance(profile_settings, dict) else {}
    default_slug = (settings.get("model") or "").split("[")[0]
    models = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("slug"):
            continue
        slug = entry["slug"]
        models.append({"slug": slug, "label": entry.get("description") or entry.get("display_name") or "",
                       "reasoning": entry.get("default_reasoning_level") or "",
                       "codex": entry, "claude": derive_claude_mapping(settings, default_slug or slug, slug)})
    if not models:
        return None
    slugs = [m["slug"] for m in models]
    return {"version": 2, "provider": "", "default": default_slug if default_slug in slugs else slugs[0],
            "models": models, "settings": settings}


def find_entry(catalog, slug):
    if not catalog or not slug:
        return None
    for entry in catalog["models"]:
        if entry["slug"] == slug:
            return entry
    return None


def resolve_model(catalog, token):
    entries = catalog["models"]
    token = str(token).strip()
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(entries):
            return entries[index - 1]["slug"]
        raise ValueError(f"model index out of range: {token}")
    for entry in entries:
        if entry["slug"].lower() == token.lower():
            return entry["slug"]
    matches = [e["slug"] for e in entries if e["slug"].lower().startswith(token.lower())]
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(e["slug"] for e in entries)
    raise ValueError(f"unknown model '{token}'; this profile offers: {available}")


def pick_model(catalog, requested, fallback, assume_yes=False, force=False):
    """Choose the model to activate (no prompting when requested/non-interactive)."""
    if not catalog:
        return requested
    entries = catalog["models"]
    slugs = [e["slug"] for e in entries]
    if requested:
        return resolve_model(catalog, requested)
    if fallback not in slugs:
        fallback = catalog["default"] if catalog["default"] in slugs else slugs[0]
    if assume_yes or (not force and (not sys.stdin.isatty() or len(entries) == 1)):
        return fallback
    print(f"This profile provides {len(entries)} models:")
    for index, entry in enumerate(entries, 1):
        mark = "  [default]" if entry["slug"] == fallback else ""
        print(f"  {index}) {entry['slug']:<28} {entry.get('label', '')}{mark}")
    while True:
        try:
            raw = input(f"Select model [1-{len(entries)}, Enter={fallback}, q=cancel]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise KeyboardInterrupt
        if not raw:
            return fallback
        if raw.lower() in ("q", "quit", "cancel"):
            raise KeyboardInterrupt
        try:
            return resolve_model(catalog, raw)
        except ValueError as error:
            print(f"  {error}")


# ---------------------------------------------------------------- switching
def catalog_target(text, catalog_path):
    """Honour a profile's own model_catalog_json path when it declares one."""
    declared = top_level_get(text, "model_catalog_json") if text else None
    if declared:
        return expand(declared)
    return catalog_path


def build_codex_catalog(d, catalog, include_profile_entries=True):
    """Union of the profile's catalog file and every selectable model entry."""
    entries = {}
    if include_profile_entries:
        try:
            raw = json.loads((d / "codex-models.json").read_text())
            for entry in (raw.get("models") if isinstance(raw, dict) else raw) or []:
                if isinstance(entry, dict) and entry.get("slug"):
                    entries[entry["slug"]] = entry
        except (OSError, ValueError, AttributeError):
            pass
    for entry in catalog["models"]:
        if isinstance(entry.get("codex"), dict):
            entries[entry["slug"]] = entry["codex"]
    if not entries:
        return None
    ordered = [entries[e["slug"]] for e in catalog["models"] if e["slug"] in entries]
    ordered += [v for k, v in sorted(entries.items()) if v not in ordered]
    return {"models": ordered}


def compute_changes(d, slug, pin=False, patch_url=None):
    """Return {path: text-or-None}: None means "remove this stale file".

    ``patch_url`` repoints Codex at the local gateway patch proxy.  It only ever affects
    the text written to the live config: the profile keeps the provider's real endpoint.
    """
    changes, notes = {}, []
    catalog = load_catalog(d)
    entry = find_entry(catalog, slug)
    state = load_state()
    previously_written = state.get("files") if isinstance(state.get("files"), dict) else {}

    text = read_text(d / "codex-config.toml")
    codex_catalog = None
    # A model that is used but not listed (open-ended custom endpoint, or a legacy
    # profile without a model list) gets a generated entry so that both agents are
    # pointed at the requested model instead of falling back to the snapshot.
    if slug and entry is None:
        entry = adhoc_model(slug)
        if catalog and catalog.get("open_ended"):
            catalog = dict(catalog, models=catalog["models"] + [entry])
    if text is not None:
        # Every profile publishes its models, so the picker inside Codex lists
        # them too.  --pin publishes only the activated one, which removes the
        # other entries from Codex's own picker.
        if catalog:
            if pin and entry:
                codex_catalog = build_codex_catalog(d, dict(catalog, models=[entry]),
                                                    include_profile_entries=False)
            else:
                codex_catalog = build_codex_catalog(d, catalog)
        if codex_catalog is not None:
            catalog_path = catalog_target(text, CODEX_MODELS)
            changes[catalog_path] = json.dumps(codex_catalog, indent=2) + "\n"
            if top_level_get(text, "model_catalog_json") is None:
                text = top_level_set(text, "model_catalog_json", display_path(catalog_path))
        elif top_level_get(text, "model_catalog_json"):
            declared = catalog_target(text, CODEX_MODELS)
            if not declared.exists():
                # Never leave the configuration pointing at a catalogue that is not there.
                text = top_level_drop(text, "model_catalog_json")
                notes.append("dropped the model_catalog_json reference to the missing "
                             f"{display_path(declared)}; Codex uses its built-in model list")
        if slug:
            text = top_level_set(text, "model", slug)
            if entry and entry.get("reasoning"):
                text = top_level_set(text, "model_reasoning_effort", entry["reasoning"])
        elif entry:
            text = top_level_set(text, "model", entry["slug"])
        if patch_url:
            text = set_provider_base_url(text, patch_url)
        changes[CODEX] = text
    elif (d / "codex-auth.json").exists() or (d / "codex-models.json").exists():
        notes.append("profile has Codex side files but no codex-config.toml")

    claude_file = d / "claude-settings.json"
    if claude_file.exists():
        profile_settings = parse_json_file(claude_file)
        live = parse_json_file(CLAUDE)
        changes[CLAUDE] = json.dumps(build_claude_settings(live, profile_settings, entry), indent=2) + "\n"
    else:
        notes.append("profile does not configure Claude Code; ~/.claude/settings.json is left untouched")

    if (d / "codex-config.toml").exists() and not (d / "codex-auth.json").exists():
        notes.append("profile has no codex-auth.json; re-run 'ai-switch init' after setting the API key")

    # A profile that does not publish a catalogue must not inherit the previous
    # provider's one: a leftover catalogue makes Codex treat the new provider's
    # models as unknown, and sessions using them cannot be resumed.
    if text is not None and codex_catalog is None:
        stale = CODEX_MODELS
        recorded = previously_written.get(str(stale))
        current = read_text(stale)
        if recorded and recorded != "removed" and stale not in changes:
            if current is not None and digest(current) == recorded:
                changes[stale] = None
                notes.append(f"removes the stale {display_path(stale)} left over from the previous provider")
            elif current is not None:
                notes.append(f"{display_path(stale)} was modified by hand and is left untouched")
    return changes, notes


def parse_json_file(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def backup_dir():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "backups" / stamp
    counter = 1
    while backup.exists():
        counter += 1
        backup = ROOT / "backups" / f"{stamp}-{counter}"
    return backup


def keep_copy(backup, path):
    backup.mkdir(parents=True, exist_ok=True)
    secure(backup)
    shutil.copy2(path, backup / path.name)
    secure(backup / path.name)


def write_detail(path, data):
    """Short description of what a written file now contains."""
    name = path.name
    if name == "models.json":
        try:
            models = json.loads(data).get("models") or []
            return f"{len(models)} model(s)"
        except ValueError:
            return ""
    if name == "config.toml":
        model = top_level_get(data, "model")
        effort = top_level_get(data, "model_reasoning_effort")
        return " · ".join(part for part in (f"model {model}" if model else "",
                                            f"effort {effort}" if effort else "") if part)
    if name == "settings.json":
        try:
            settings = json.loads(data)
        except ValueError:
            return ""
        pins = claude_model_pins(settings)
        distinct = sorted(set(pins.values()))
        return f"opus/sonnet/haiku → {distinct[0]}" if len(distinct) == 1 else \
            f"{len(distinct)} Claude category mappings"
    if name == "auth.json":
        return "API key stored"
    return ""


def planned_steps(changes):
    """How many progress steps apply_changes will report: backup + files + state."""
    backup_needed = any(changes[path] is not None and path.exists() for path in changes)
    return len(changes) + 1 + (1 if backup_needed else 0)


def apply_changes(changes, name, slug, dry_run=False, progress=None):
    backup = backup_dir()
    written, backed_up = {}, 0
    plan = sorted(changes, key=str)
    if dry_run:
        for path in plan:
            kind = "remove" if changes[path] is None else "write"
            print(f"dry-run: would {kind} {display_path(path)}")
        return None
    # every file that will be overwritten is copied out of the way first, so a
    # failure halfway through never leaves the agents without their previous files
    to_copy = [path for path in plan if changes[path] is not None and path.exists()]
    if to_copy and progress:
        progress.animate(f"backing up {len(to_copy)} file(s)")
    for path in to_copy:
        keep_copy(backup, path)
        backed_up += 1
    if to_copy and progress:
        progress.step(f"backup {backed_up} file(s)", display_path(backup))
    for path in plan:
        data = changes[path]
        if is_history_path(path):
            raise ValueError(f"refusing to manage agent history file: {path}")
        if data is None:
            if progress:
                progress.animate(f"removing {display_path(path)}")
            if path.exists():
                removed = backup / "removed"
                removed.mkdir(parents=True, exist_ok=True)
                secure(removed)
                shutil.move(str(path), str(removed / path.name))
                backed_up += 1
                written[str(path)] = "removed"
            if progress:
                progress.step(f"remove {display_path(path)}", "copy kept in the backup")
            else:
                print(f"Removed {display_path(path)}")
            continue
        if progress:
            progress.animate(f"writing {display_path(path)}")
        write_atomic(path, data)
        written[str(path)] = digest(data)
        if progress:
            progress.step(f"write {display_path(path)}", write_detail(path, data))
        else:
            print(f"Wrote {display_path(path)}")
    if progress:
        progress.animate("saving profile state")
    state = load_state()
    models = state.get("models") if isinstance(state.get("models"), dict) else {}
    if slug:
        models[name] = slug
    state.update({"profile": name, "models": models, "files": written, "updated": timestamp()})
    save_state(state)
    write_atomic(CURRENT, name + "\n")
    if progress:
        progress.step("save profile state", f"{name} is now the active profile")
    return backup if backed_up else None


def codex_health_warnings(check_codex=True):
    """Cheap pre-switch check so lost sessions are explained, not mysterious."""
    warnings = []
    if check_codex:
        for db in sorted(CODEX_DIR.glob("state_*.sqlite")) + sorted(CODEX_DIR.glob("thread_history_*.sqlite")):
            status, detail = sqlite_status(db)
            if status != "ok":
                warnings.append(f"Codex runtime DB {db.name} is unusable ({detail}) - sessions may be missing "
                                "from the resume picker; run 'ai-switch doctor --fix'")
    if running_agents():
        warnings.append("running now: " + running_agents_summary() +
                        " - restart them after switching, a running agent rewrites its config on exit")
    return warnings


# ---------------------------------------------------------------- gateway patch
# Some gateways translate Codex's Responses API into Chat completions, and in doing so
# break every turn in which Codex writes its assistant ``message`` item *between* the
# turn's tool calls and their results.  Codex always writes that item - empty when the
# model said nothing before calling a tool - and a translator that emits it as its own
# chat message leaves the ``tool_calls`` message without the ``tool`` messages that must
# follow it directly, so the next request is rejected with "An assistant message with
# 'tool_calls' must be followed by tool messages" and the session dies.  For a profile
# whose endpoint behaves that way ``ai-switch use`` starts a small local proxy that moves
# the message back in front of the calls before forwarding the request, points Codex at
# it, and stops it again when another profile is activated.
PATCH_PORT = 8791
PATCH_HEALTH = "/__ai-switch-patch"
PATCH_START_TIMEOUT = 10
PATCH_READ_TIMEOUT = 900
PATCH_LOG_LIMIT = 1 << 20
# The proxy process learns where to log from argv: its environment says nothing about
# this tool's home directory.  The parent leaves this None.
PATCH_LOG = None
PATCH_CALL_ITEMS = ("function_call", "custom_tool_call")
PATCH_RESULT_ITEMS = ("function_call_output", "custom_tool_call_output")
GATEWAY_PATCHES = {
    "paratera.com": "Paratera translates Responses into Chat completions and splits a turn's "
                    "tool calls from their results",
}


def patch_log_path():
    return ROOT / "patch.log"


def _host_without_port(url):
    return host_of(url).rsplit("@", 1)[-1].split(":")[0].strip().lower()


def provider_wire_api(text):
    """The ``wire_api`` of the profile's provider section (Codex's only mode is responses)."""
    _, tail = split_toml(text or "")
    found = re.search(r'(?m)^wire_api[ \t]*=[ \t]*["\']([^"\']+)', tail)
    return found.group(1) if found else None


def gateway_patch_for(base_url):
    """The patch a profile's endpoint needs, or None - matched on the host suffix."""
    host = _host_without_port(base_url or "")
    if not host:
        return None
    for suffix, reason in GATEWAY_PATCHES.items():
        if host == suffix or host.endswith("." + suffix):
            return {"host": host, "reason": reason}
    return None


def patch_needed(codex_config, base_url=None):
    """Whether activating this profile has to route Codex through the proxy."""
    text = codex_config or ""
    url = _provider_base_url(text) if base_url is None else base_url
    patch = gateway_patch_for(url) if url else None
    if patch is None:
        return None
    # Only the Responses API is translated by the gateway; a profile that asked for
    # anything else is forwarded untouched and needs no proxy.
    return patch if (provider_wire_api(text) or "responses") == "responses" else None


def set_provider_base_url(text, url):
    """Repoint the provider section at the local proxy (the profile keeps the real one)."""
    head, tail = split_toml(text)
    replaced, count = re.subn(r'(?m)^(base_url[ \t]*=[ \t]*)(["\'])[^"\']*\2',
                              lambda match: match.group(1) + match.group(2) + url + match.group(2),
                              tail, count=1)
    if not count:
        return text
    return head + replaced


def _message_text(item):
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))


def rewrite_tool_items(items):
    """Move a tool turn's assistant message in front of the turn's calls.

    Returns ``(items, moved, dropped)``: the reordered list, how many messages were moved
    and how many empty ones were removed.  Anything that is not exactly
    ``calls + assistant message + results`` is left untouched.
    """
    if not isinstance(items, list):
        return items, 0, 0
    out, moved, dropped, index = [], 0, 0, 0
    while index < len(items):
        item = items[index]
        if not isinstance(item, dict) or item.get("type") not in PATCH_CALL_ITEMS:
            out.append(item)
            index += 1
            continue
        end = index
        while end < len(items) and isinstance(items[end], dict) \
                and items[end].get("type") in PATCH_CALL_ITEMS:
            end += 1
        calls = items[index:end]
        after = end
        messages = []
        while after < len(items) and isinstance(items[after], dict) \
                and items[after].get("type") == "message" and items[after].get("role") == "assistant":
            messages.append(items[after])
            after += 1
        completed = after < len(items) and isinstance(items[after], dict) \
            and items[after].get("type") in PATCH_RESULT_ITEMS
        if messages and completed:
            for message in messages:
                if _message_text(message).strip():
                    out.append(message)
                    moved += 1
                else:
                    dropped += 1
            out.extend(calls)
            index = after
            continue
        out.extend(calls)
        index = end
    return out, moved, dropped


class PatchHandler(BaseHTTPRequestHandler):
    """Forwards to the real endpoint, rewriting ``/responses`` bodies on the way through."""

    protocol_version = "HTTP/1.1"
    # Hop-by-hop headers plus anything that would misdescribe the body we send upstream.
    STRIPPED = ("host", "content-length", "connection", "transfer-encoding", "content-encoding",
                "accept-encoding", "expect")

    def log_message(self, *args):
        pass                      # never log request lines: they carry the API key

    def _reply_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == PATCH_HEALTH:
            self._reply_json({"ai_switch_patch": VERSION, "pid": os.getpid(),
                              "upstream": self.server.upstream})
            return
        self.forward("GET")

    def do_POST(self):
        self.forward("POST")

    def read_body(self):
        """The request body, whether the client sent a length or chunked it."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            parts = []
            while True:
                size = int(self.rfile.readline(65536).split(b";")[0].strip() or b"0", 16)
                if not size:
                    self.rfile.readline(65536)          # the trailer's blank line
                    break
                parts.append(self.rfile.read(size))
                self.rfile.read(2)                      # CRLF after the chunk
            return b"".join(parts)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def patched_body(self, body):
        try:
            document = json.loads(body)
        except ValueError:
            return body, 0, 0
        if not isinstance(document, dict) or not isinstance(document.get("input"), list):
            return body, 0, 0
        items, moved, dropped = rewrite_tool_items(document["input"])
        if not moved and not dropped:
            return body, 0, 0
        document["input"] = items
        return json.dumps(document).encode(), moved, dropped

    def forward(self, method):
        body = self.read_body()
        moved = dropped = 0
        if body and self.path.split("?")[0].rstrip("/").endswith("/responses"):
            body, moved, dropped = self.patched_body(body)
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in self.STRIPPED}
        headers.setdefault("User-Agent", f"ai-switch/{VERSION}")
        request = urllib.request.Request(self.server.upstream + self.path,
                                         data=body or None, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=PATCH_READ_TIMEOUT)
        except urllib.error.HTTPError as error:
            response = error
        except Exception as error:                      # upstream unreachable, DNS, timeout
            patch_log(f"upstream unreachable: {type(error).__name__}: {error}")
            try:
                self._reply_json({"error": {"message": f"ai-switch patch proxy: {error}"}}, status=502)
            except OSError:
                pass
            return
        patch_log(f"{method} {self.path} items edited: moved={moved} dropped={dropped} "
                  f"-> {getattr(response, 'status', '?')}")
        try:
            self.relay(response)
        except OSError:                                  # the agent hung up mid-stream
            pass
        finally:
            response.close()

    def relay(self, response):
        status = getattr(response, "status", 200)
        headers = response.headers
        self.send_response(status)
        for name in ("Content-Type", "Content-Encoding"):
            value = headers.get(name)
            if value:
                self.send_header(name, value)
        length = headers.get("Content-Length")
        if length is not None:
            self.send_header("Content-Length", length)
            self.end_headers()
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            return
        # Streaming (SSE) arrives without a length: chunked, flushed as it comes, and the
        # connection is closed afterwards rather than left half-open.
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        while True:
            chunk = response.read(2048)
            if not chunk:
                break
            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def patch_log(line):
    path = PATCH_LOG or patch_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as handle:
            handle.write(f"{timestamp()} {line}\n")
    except OSError:
        pass


def shim_main(argv):
    """Entry point of the proxy process: ``shim_main([port, upstream, logfile?])``."""
    global PATCH_LOG
    if len(argv) < 2:
        print("usage: ai_switch.shim_main PORT UPSTREAM [LOGFILE]", file=sys.stderr)
        return 2
    port, upstream = int(argv[0]), argv[1].rstrip("/")
    if len(argv) > 2:
        PATCH_LOG = Path(argv[2])
    trim_patch_log(PATCH_LOG or patch_log_path())
    server = ThreadingHTTPServer(("127.0.0.1", port), PatchHandler)
    server.daemon_threads = True
    server.upstream = upstream
    patch_log(f"patch proxy listening on 127.0.0.1:{port} -> {upstream} (pid {os.getpid()})")
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    server.serve_forever()
    patch_log("patch proxy stopped")
    return 0


def trim_patch_log(path):
    try:
        if path.exists() and path.stat().st_size > PATCH_LOG_LIMIT:
            path.replace(path.parent / (path.name + ".1"))
    except OSError:
        pass


def patch_health(port, timeout=0.6):
    """The proxy's own status endpoint, which also proves it is the one we started."""
    if not port:
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}{PATCH_HEALTH}",
                                    timeout=timeout) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("ai_switch_patch") else None


def patch_record():
    record = load_state().get("patch")
    return record if isinstance(record, dict) else {}


def remember_patch(record):
    state = load_state()
    if record:
        state["patch"] = record
    else:
        state.pop("patch", None)
    save_state(state)


def patch_status():
    """What the proxy is doing now: ``running``, its port and the endpoint it serves."""
    record = patch_record()
    health = patch_health(record.get("port"))
    if not health:
        return {"running": False, "port": record.get("port"), "upstream": record.get("upstream"),
                "stale": bool(record)}
    return {"running": True, "port": record.get("port"), "pid": health.get("pid"),
            "upstream": health.get("upstream") or record.get("upstream"), "stale": False}


def wait_for_patch(port, timeout=PATCH_START_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if patch_health(port, timeout=0.5):
            return True
        time.sleep(0.05)
    return False


def free_patch_port(preferred=PATCH_PORT):
    """The preferred port, or whatever the OS hands out when something else holds it."""
    for candidate in (preferred, 0):
        try:
            with socket.socket() as probe:
                # The proxy binds with SO_REUSEADDR too, so probe the same way: a port its
                # own previous instance left in TIME_WAIT is still usable, and one that
                # another process is listening on is not.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", candidate))
                port = probe.getsockname()[1]
            if port:
                return port
        except OSError:
            continue
    raise OSError("no free TCP port on 127.0.0.1 for the gateway patch")


def start_patch(upstream):
    """Launch the proxy for ``upstream`` and wait until it answers; returns its record."""
    log_path = patch_log_path()
    trim_patch_log(log_path)
    port = free_patch_port()
    bootstrap = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "from ai_switch import shim_main; sys.exit(shim_main(sys.argv[2:]))")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    secure(log_path.parent)
    with open(log_path, "ab") as sink:
        process = subprocess.Popen(
            [sys.executable, "-c", bootstrap, str(Path(__file__).resolve().parent),
             str(port), upstream, str(log_path)],
            stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, start_new_session=True)
    if not wait_for_patch(port):
        stop_process(process)
        raise ValueError(f"the gateway patch proxy did not come up on 127.0.0.1:{port}; "
                         f"see {display_path(log_path)}")
    record = {"pid": process.pid, "port": port, "upstream": upstream, "version": VERSION,
              "started": timestamp()}
    remember_patch(record)
    return record


def stop_process(process):
    try:
        process.terminate()
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def stop_patch():
    """Stop the proxy - whether or not state.json still remembers it. Returns its ports."""
    ports = []
    for record in (patch_record(), {"port": PATCH_PORT}):
        port = record.get("port")
        health = patch_health(port)
        pid = (health or {}).get("pid")
        if not pid or pid == os.getpid():
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            continue
        ports.append(port)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not patch_health(port, timeout=0.2):
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (OSError, ValueError):
                pass
    if patch_record():
        remember_patch(None)
    return ports


def activate_patch_for(d, name, dry_run=False, enabled=True):
    """Decide the proxy's fate for the profile being activated.

    Returns the base URL Codex should use, or None to leave the profile's own endpoint in
    place.  A running proxy for the same endpoint is reused; one for a different endpoint,
    from another version, or no longer wanted is stopped first.
    """
    codex_config = read_text(d / "codex-config.toml") or ""
    patch = patch_needed(codex_config) if enabled else None
    if patch is None:
        if dry_run:
            live = patch_status()
            if live["running"]:
                print(f"dry-run: would stop the gateway patch proxy on 127.0.0.1:{live['port']}",
                      file=sys.stderr)
            return None
        for port in stop_patch():
            print(f"Note: stopped the gateway patch proxy on 127.0.0.1:{port}; '{name}' does "
                  "not need it.", file=sys.stderr)
        return None
    if dry_run:
        record = patch_record() if patch_status()["running"] else {}
        print(f"dry-run: would route Codex through the {patch['host']} patch proxy on "
              f"127.0.0.1:{record.get('port') or PATCH_PORT}", file=sys.stderr)
        return f"http://127.0.0.1:{record.get('port') or PATCH_PORT}"
    upstream = _provider_base_url(codex_config)
    record, live = patch_record(), patch_status()
    if live["running"] and record.get("upstream") == upstream and record.get("version") == VERSION:
        return f"http://127.0.0.1:{live['port']}"
    if live["running"] or record:
        stop_patch()
    record = start_patch(upstream)
    print(f"Note: Codex will use the {patch['host']} patch proxy on 127.0.0.1:{record['port']} "
          f"({patch['reason']}).", file=sys.stderr)
    return f"http://127.0.0.1:{record['port']}"


def cmd_use(args):
    name = args.name
    d = profile(name)
    if not d.is_dir():
        raise FileNotFoundError(f"profile not found: {name}")
    if not any((d / profile_file).exists() for profile_file in PROFILE_FILES):
        raise ValueError(f"profile '{name}' has no Codex or Claude configuration to activate")
    catalog = load_catalog(d)
    fallback = last_model(name) or (catalog or {}).get("default")
    slug = args.model or fallback
    if args.model and catalog and catalog.get("open_ended"):
        try:
            slug = resolve_model(catalog, args.model)
        except ValueError:
            slug = args.model
            print(f"Note: '{args.model}' is not in the profile's model list; using it as-is.", file=sys.stderr)
    elif args.model and catalog:
        slug = resolve_model(catalog, args.model)
    elif catalog and args.choose:
        # Picking a model here only sets the default for new sessions; every model
        # of the profile is published, so it can also be changed inside the agent.
        slug = pick_model(catalog, None, fallback, force=True)
    if slug and catalog is None:
        print(f"Note: profile '{name}' has no model list, using '{slug}' as-is.", file=sys.stderr)
    patch_url = activate_patch_for(d, name, dry_run=args.dry_run,
                                   enabled=not getattr(args, "no_patch", False))
    changes, notes = compute_changes(d, slug, pin=args.pin, patch_url=patch_url)
    for note in notes:
        print(f"Note: {note}", file=sys.stderr)
    plain = getattr(args, "plain", False)
    style = Style(force_off=plain)
    progress = Progress(planned_steps(changes), style, animate=not plain)
    if not args.dry_run and progress.enabled:
        progress.animate(f"activating {name}")
    if not progress.enabled:
        progress = None          # keep the plain, script-friendly output off a terminal
    backup = apply_changes(changes, name, slug, dry_run=args.dry_run, progress=progress)
    if not args.dry_run:
        backup_text = display_path(backup) if backup else "nothing to back up (no files existed yet)"
        if progress:
            progress.sweep("activation complete")
            profile_tail = f"  ·  only {slug} published" if args.pin and slug else ""
            models_tail = f"{len(catalog['models'])} published  ·  switch inside codex with /model" \
                if catalog else "no model list"
            plain_models = models_tail
            styled_models = (style(f"{len(catalog['models'])} published", "green")
                             + style("  ·  switch inside codex with /model", "grey")) if catalog \
                else style("no model list", "grey")
            if not catalog:
                plain_models = "no model list"
            rows = [("profile", name + profile_tail,
                     style(name, "bold", "cyan") + (style(profile_tail, "grey") if profile_tail else "")),
                    ("default", str(slug or "-"), style(str(slug), "bold", "green")),
                    ("models", plain_models, styled_models),
                    ("backup", backup_text, style(backup_text, "grey")),
                    ("next", "restart claude/codex to reload", style("restart claude/codex to reload", "yellow"))]
            progress.result(f"{name} is active", rows)
        else:
            print(f"Active profile: {name}" + (f"\nDefault model: {slug} (new sessions)" if slug else ""))
            if args.pin and slug:
                print(f"Pinned: only '{slug}' is published, so Codex cannot switch to another model.")
            elif catalog:
                print(f"Published models: {len(catalog['models'])} - switch any time with /model inside "
                      "codex (Claude Code uses its Opus/Sonnet/Haiku mappings), or change the default "
                      "with -m/--model.")
            print(f"Backup: {backup}" if backup else "Backup: nothing to back up (no files existed yet)")
            print("Restart claude/codex so they reload their configuration.")
        if not args.no_check:
            for warning in codex_health_warnings((d / "codex-config.toml").exists()):
                print(style("Warning: ", "yellow") + warning, file=sys.stderr)
    return 0


# ---------------------------------------------------------------- commands
def cmd_init(args):
    d = profile(args.name)
    d.mkdir(parents=True, exist_ok=False)
    secure(d)
    copied = []
    for profile_file, target in targets().items():
        if target.exists():
            shutil.copy2(target, d / profile_file)
            secure(d / profile_file)
            copied.append(profile_file)
    catalog, settings = None, parse_json_file(d / "claude-settings.json")
    if not args.no_import:
        catalog = load_catalog(d, settings)
        if catalog:
            models = []
            for entry in catalog["models"]:
                models.append({"slug": entry["slug"], "label": entry.get("label", ""),
                               "reasoning": entry.get("reasoning", ""),
                               "codex": entry.get("codex"), "claude": entry.get("claude")})
            write_atomic(d / "models.json", json.dumps({"version": 2, "provider": "",
                                                        "default": catalog["default"], "models": models},
                                                       indent=2) + "\n")
    write_atomic(d / "profile.json", json.dumps({"description": args.description,
                                                 "created": timestamp(), "files": copied}, indent=2) + "\n")
    print(f"Created profile: {args.name}")
    if catalog:
        print(f"Imported {len(catalog['models'])} model(s): " + ", ".join(m["slug"] for m in catalog["models"]))
    print(f"Activate it with: ai-switch use {args.name}")
    return 0


def cmd_list(_):
    current = active_profile()
    for d in sorted(PROFILES.iterdir() if PROFILES.exists() else []):
        if not d.is_dir():
            continue
        desc, clients, details = summary(d)
        print(f"{'*' if d.name == current else ' '} {d.name:<16} {clients:<12} {desc} [{details}]")
    return 0


def summary(d):
    meta = profile_meta(d)
    clients, details = [], []
    text = read_text(d / "codex-config.toml")
    if text is not None:
        clients.append("Codex")
        model = top_level_get(text, "model")
        base = top_level_get(text, "base_url") or _provider_base_url(text)
        if model:
            details.append("Codex model=" + str(model))
        if base:
            details.append("Codex endpoint=" + host_of(base))
    settings = parse_json_file(d / "claude-settings.json")
    if settings:
        clients.append("Claude")
        if settings.get("model"):
            details.append("Claude model=" + str(settings["model"]))
        base = (settings.get("env") or {}).get("ANTHROPIC_BASE_URL")
        if base:
            details.append("Claude endpoint=" + host_of(base))
    catalog = load_catalog(d, settings)
    if catalog:
        details.append(f"{len(catalog['models'])} model(s)")
    return (meta.get("description") or "No description", ",".join(clients) or "No config",
            "; ".join(details) or "No automatic summary")


def _profile_host(d, settings):
    text = read_text(d / "codex-config.toml") or ""
    base = _provider_base_url(text) or (settings.get("env") or {}).get("ANTHROPIC_BASE_URL")
    return host_of(base) if base else ""


def _provider_base_url(text):
    _, tail = split_toml(text)
    found = re.search(r'(?m)^base_url[ \t]*=[ \t]*["\']([^"\']+)', tail)
    return found.group(1) if found else None


def host_of(url):
    text = str(url)
    return text.split("/")[2] if "://" in text and len(text.split("/")) > 2 else text


def claude_model_pins(settings):
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    return {key: env.get(key) for key in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                                          "ANTHROPIC_DEFAULT_HAIKU_MODEL") if env.get(key)}


def cmd_models(args):
    name = args.name or active_profile()
    if not name:
        raise ValueError("no active profile; pass a profile name")
    d = profile(name)
    if not d.is_dir():
        raise FileNotFoundError(f"profile not found: {name}")
    catalog = load_catalog(d, parse_json_file(d / "claude-settings.json"))
    current = last_model(name) if name == active_profile() else None
    if not catalog:
        text = read_text(d / "codex-config.toml") or ""
        model = top_level_get(text, "model")
        if args.json:
            print(json.dumps({"profile": name, "models": ([model] if model else [])}, indent=2))
        else:
            print(f"Profile '{name}' has no model list." + (f" Codex model: {model}" if model else ""))
            print("Add one with 'ai-switch add' or 'ai-switch upgrade %s'." % name)
        return 0
    if args.json:
        print(json.dumps({"profile": name, "default": catalog["default"], "active": current,
                          "models": [{"slug": e["slug"], "label": e.get("label", ""),
                                      "claude_model": (e.get("claude") or {}).get("model")}
                                     for e in catalog["models"]]}, indent=2))
        return 0
    print(f"Profile '{name}' - default {catalog['default']}, {len(catalog['models'])} model(s):")
    for index, entry in enumerate(catalog["models"], 1):
        marks = []
        if entry["slug"] == catalog["default"]:
            marks.append("default")
        if entry["slug"] == current:
            marks.append("active")
        claude_model = (entry.get("claude") or {}).get("model")
        suffix = f"  -> Claude {claude_model}" if claude_model else ""
        print(f"  {index}) {entry['slug']:<30} {entry.get('label', '')}"
              f"{('  [' + ', '.join(marks) + ']') if marks else ''}{suffix}")
    return 0


def cmd_current(args):
    """Show every configured profile, its models, and the live state."""
    name = active_profile()
    if getattr(args, "plain", False):
        print(name or "(none)")
        return 0
    style = Style()
    profiles = [p for p in sorted(PROFILES.iterdir()) if p.is_dir()] if PROFILES.exists() else []
    if not profiles:
        print(style("no profiles yet", "yellow") + style("  ·  create one with 'ai-switch add'", "grey"))
        return 0
    published = len(parse_json_file(CODEX_MODELS).get("models") or [])
    live_model = top_level_get(read_text(CODEX) or "", "model")
    print(style("ai-switch", "bold") + style(f"  ·  {len(profiles)} profile(s)  ·  "
                                             f"{display_path(ROOT)}", "grey"))
    print(style.rule())
    for d in profiles:
        active = d.name == name
        marker = style("●", "green") if active else style("○", "grey")
        head = style(d.name, "bold", "cyan") if active else style(d.name, "bold")
        description = profile_meta(d).get("description") or ""
        tail = style("active", "green") if active else ""
        print(f"  {marker} {head}   {style(description, 'grey')}   {tail}".rstrip())
        settings = parse_json_file(d / "claude-settings.json")
        catalog = load_catalog(d, settings)
        codex_model = top_level_get(read_text(d / "codex-config.toml") or "", "model")
        claude_model = settings.get("model")
        host = _profile_host(d, settings)
        rows = []
        if codex_model:
            published_for_this = f"{len(catalog['models'])} models" if catalog else "no model list"
            rows.append(("Codex ", f"{style(str(codex_model), 'green')}  {style('· ' + published_for_this, 'grey')}"))
        if claude_model:
            pins = claude_model_pins(settings)
            distinct = sorted(set(pins.values()))
            if len(distinct) == 1:
                mapping = f"opus/sonnet/haiku → {distinct[0]}"
            elif distinct:
                mapping = f"{len(distinct)} Claude category mappings"
            else:
                mapping = ""
            rows.append(("Claude", f"{style(str(claude_model), 'green')}"
                                   + (style('  · ' + mapping, 'grey') if mapping else "")))
        if host:
            rows.append(("host  ", style(host, "grey")))
        if catalog:
            slugs = [entry["slug"] + (" ★" if entry["slug"] == catalog["default"] else "")
                     for entry in catalog["models"]]
            first = True
            for line in textwrap.wrap(", ".join(slugs), width=76) or [""]:
                rows.append(("models" if first else "      ", style(line, "grey")))
                first = False
        for label, value in rows:
            print(f"      {style(label, 'grey')}  {value}")
        if active:
            live = []
            if live_model and live_model != codex_model:
                live.append(style(f"live Codex model is {live_model} (a running codex session changed it)",
                                  "yellow"))
            elif live_model:
                live.append(style(f"live Codex config uses {live_model}", "grey"))
            if published and catalog and published != len(catalog["models"]):
                live.append(style(f"{published} model(s) published to {display_path(CODEX_MODELS)}", "grey"))
            proxy = patch_status()
            if proxy["running"]:
                live.append(style(f"gateway patch proxy on 127.0.0.1:{proxy['port']} → "
                                  f"{proxy.get('upstream')}", "grey"))
            elif proxy.get("stale"):
                live.append(style("gateway patch proxy is not running - re-run "
                                  f"'ai-switch use {name}' to restart it", "yellow"))
            for line in live:
                print(f"      {style('state ', 'grey')}  {line}")
        print()
    return 0


def cmd_describe(args):
    d = profile(args.name)
    if not d.is_dir():
        raise FileNotFoundError(f"profile not found: {args.name}")
    meta = profile_meta(d)
    meta.update({"description": args.text, "updated": timestamp()})
    write_atomic(d / "profile.json", json.dumps(meta, indent=2) + "\n")
    print(f"Updated description: {args.name}")
    return 0


def cmd_upgrade(args):
    """Give a profile written by an older release an explicit model list."""
    d = profile(args.name)
    if not d.is_dir():
        raise FileNotFoundError(f"profile not found: {args.name}")
    if (d / "models.json").exists() and not args.force:
        print(f"Profile '{args.name}' already has models.json (use --force to rebuild it).")
        return 0
    settings = parse_json_file(d / "claude-settings.json")
    catalog = load_catalog(d, settings)
    if not catalog:
        raise ValueError(f"profile '{args.name}' has no codex-models.json to build a model list from")
    models = []
    for entry in catalog["models"]:
        models.append({"slug": entry["slug"], "label": entry.get("label", ""), "reasoning": entry.get("reasoning", ""),
                       "codex": entry.get("codex"), "claude": entry.get("claude")})
    write_atomic(d / "models.json", json.dumps({"version": 2, "provider": catalog.get("provider", ""),
                                               "default": catalog["default"], "models": models}, indent=2) + "\n")
    print(f"Upgraded '{args.name}': {len(models)} model(s) - " + ", ".join(m["slug"] for m in models))
    print(f"Pick one with: ai-switch use {args.name}")
    return 0


def _ask(prompt, default=""):
    suffix = f" (default: {default})" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise KeyboardInterrupt
    return answer or default


def ask_secret(prompt="API key: "):
    """Read a secret from the same stream as the other prompts, without echo.

    ``getpass`` opens /dev/tty, which breaks piped/scripted input (the answers
    already buffered on stdin are invisible to it) and fails outright when no
    terminal is attached.  Set AI_SWITCH_API_KEY to skip the prompt entirely.
    """
    override = os.environ.get("AI_SWITCH_API_KEY")
    if override:
        return override.strip()
    try:
        import termios
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd) if sys.stdin.isatty() else None
    except (ImportError, OSError, ValueError, AttributeError):
        termios, fd, saved = None, None, None
    print(prompt, end="", flush=True)
    try:
        if saved is not None:
            quiet = list(saved)
            quiet[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
        line = sys.stdin.readline()
    finally:
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        print()
    if not line:
        raise KeyboardInterrupt
    return line.strip()


def cmd_add(_):
    print("Create a provider profile (API keys are not echoed).")
    print("At any prompt, type 'cancel' or press Ctrl-C to exit without saving.\n")
    provider = _ask("Provider [glm/deepseek/custom]", "glm").lower()
    if provider not in ("glm", "deepseek", "custom"):
        raise ValueError("provider must be glm, deepseek, or custom")
    preset = PRESETS.get(provider)
    key = ask_secret()
    if preset:
        chosen = preset["models"]
        print(f"{provider} preset offers: " + ", ".join(m["slug"] for m in chosen))
        wanted = _ask("Models to include (space or comma separated, Enter=all)", "all")
        if wanted.lower() not in ("all", ""):
            names = split_models(wanted)
            by_slug = {m["slug"]: m for m in chosen}
            chosen = []
            for token in names:
                exact = [s for s in by_slug if s.lower() == token.lower()]
                slug = exact or [s for s in by_slug if s.lower().startswith(token.lower())]
                if len(slug) != 1:
                    raise ValueError(f"unknown or ambiguous model '{token}'"
                                     f" (choose one of: {', '.join(by_slug)})")
                chosen.append(by_slug[slug[0]])
        default = _ask("Default model", chosen[0]["slug"])
        default = resolve_model({"models": chosen}, default)
        endpoint, claude_endpoint = preset["endpoint"], preset["claude_endpoint"]
        common_env = dict(preset["common_env"])
    else:
        endpoint = _ask("API endpoint URL (e.g. https://api.example.com/v1)")
        if not endpoint:
            raise ValueError("an endpoint URL is required")
        claude_endpoint = endpoint[:-3].rstrip("/") if endpoint.endswith("/v1") else endpoint.rstrip("/")
        names = split_models(_ask("Model name(s) (space or comma separated, e.g. 'Big-Model Small-Model')"))
        if not names:
            raise ValueError("at least one model name is required")
        chosen = []
        for index, name in enumerate(names):
            model = adhoc_model(name, index)
            if len(names) > 1:
                model["label"] = f"custom model (use --model {name})"
            chosen.append(model)
        default = chosen[0]["slug"]
        if len(chosen) > 1:
            print("This endpoint is open ended: any name works with "
                  f"'ai-switch use <profile> --model <name>', and the ones listed above appear in the picker.")
            default = resolve_model({"models": chosen}, _ask("Default model", chosen[0]["slug"]))
        common_env = {}
    name = _ask("Profile name")
    if not name:
        raise ValueError("a profile name is required")
    desc = _ask("Description")
    clients = _ask("Configure clients [both/codex/claude]", "both").lower()
    if clients not in ("both", "codex", "claude"):
        raise ValueError("clients must be both, codex, or claude")
    sqlite_home = ""
    if clients in ("both", "codex") and fstype_for(CODEX_DIR) in NETWORK_FS:
        suggested = f"/var/tmp/codex-sqlite-{os.environ.get('USER', 'user')}"
        print(f"\n{CODEX_DIR} is on a network filesystem ({fstype_for(CODEX_DIR)}); SQLite runtime databases "
              "can be corrupted there, which makes sessions disappear.")
        sqlite_home = _ask("Local directory for Codex runtime databases (Enter=skip)", suggested)
    d = profile(name)
    blank_profile_dir(d)
    catalog = {"version": 2, "provider": provider, "default": default, "models": chosen,
               "open_ended": provider == "custom"}
    write_atomic(d / "models.json", json.dumps(catalog, indent=2) + "\n")
    entry = find_entry(catalog, default)
    if clients in ("both", "codex"):
        if preset:
            catalog_path = CODEX_MODELS
            base = (preset["codex_template"].replace("@MODEL@", default)
                    .replace("@EFFORT@", entry.get("reasoning") or "high")
                    .replace("@CATALOG@", display_path(catalog_path))
                    .replace("@ENDPOINT@", preset["endpoint"]).replace("@KEY@", key))
        else:
            # A gateway often serves dozens of models; the catalogue holds the ones
            # you named, and ``use --model <other>`` extends it on demand.
            base = (f'model = "{default}"\nmodel_provider = "custom"\npreferred_auth_method = "apikey"\n'
                    f'forced_login_method = "api"\nmodel_catalog_json = "{display_path(CODEX_MODELS)}"\n\n'
                    f'[model_providers.custom]\nname = "custom"\n'
                    f'base_url = "{endpoint}"\nwire_api = "responses"\n'
                    f'requires_openai_auth = true\nexperimental_bearer_token = "{key}"\n')
        write_atomic(d / "codex-config.toml", base)
        write_atomic(d / "codex-models.json", json.dumps(build_codex_catalog(d, catalog), indent=2) + "\n")
        write_atomic(d / "codex-auth.json", json.dumps({"OPENAI_API_KEY": key}, indent=2) + "\n")
    if clients in ("both", "claude"):
        env = {"ANTHROPIC_BASE_URL": claude_endpoint, "ANTHROPIC_AUTH_TOKEN": key}
        env.update(common_env)
        write_atomic(d / "claude-settings.json", json.dumps(build_claude_settings(None, {"env": env}, entry),
                                                            indent=2) + "\n")
    write_atomic(d / "profile.json", json.dumps({"description": desc, "created": timestamp(),
                                                 "provider": provider, "default_model": default}, indent=2) + "\n")
    print(f"\nCreated profile: {name}")
    print(f"Activate it with: ai-switch use {name}   (choose between {len(chosen)} model(s))")
    if sqlite_home:
        # Codex reads this from the environment only: a `sqlite_home` key in
        # config.toml is accepted but ignored (verified with `codex doctor`).
        target = expand(sqlite_home)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        print(f"\nKeep Codex's runtime databases off {fstype_for(CODEX_DIR)} by exporting this before "
              f"starting codex (add it to ~/.bashrc):\n    export CODEX_SQLITE_HOME={sqlite_home}")
    return 0


# ---------------------------------------------------------------- health checks
SQLITE_COPY_LIMIT = 256 * 1024 * 1024


def _verdict(row, note=""):
    if row and row[0] == "ok":
        return "ok", "integrity ok" + note
    detail = " ".join(" ".join(str(part).split()) for part in (row or []) if part).strip() or "no result"
    return "broken", detail


def _sqlite_direct(path):
    for suffix in ("?mode=ro", "?mode=ro&immutable=1"):
        try:
            connection = sqlite3.connect(f"file:{Path(path).as_uri()}{suffix}", uri=True)
            try:
                return connection.execute("PRAGMA quick_check(1)").fetchone(), None
            finally:
                connection.close()
        except sqlite3.Error as error:
            last = str(error)
    return None, last


def _sqlite_snapshot(path):
    """Check a copy of the database (plus its WAL) in a local writable directory.

    Reading a live database can be refused on a network filesystem or a read-only
    mount; the copy is then the faithful view the agent itself gets.
    """
    try:
        size = path.stat().st_size
    except OSError as error:
        return "broken", str(error)
    sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-shm")]
    for side in sidecars:
        if side.exists():
            size += side.stat().st_size
    if size > SQLITE_COPY_LIMIT:
        return "skipped", f"too large to snapshot ({size // (1024 * 1024)} MiB)"
    try:
        with tempfile.TemporaryDirectory(prefix="ai-switch-db-") as tmp:
            copy = Path(tmp) / path.name
            shutil.copy2(path, copy)
            for side in sidecars:
                if side.exists():
                    shutil.copy2(side, Path(str(copy) + side.name[len(path.name):]))
            connection = sqlite3.connect(str(copy))
            try:
                row = connection.execute("PRAGMA quick_check(1)").fetchone()
            finally:
                connection.close()
            return _verdict(row, " (checked on a snapshot copy)")
    except (OSError, sqlite3.Error) as error:
        return "broken", str(error)


def sqlite_status(path):
    """Return ("ok"|"broken"|"missing"|"skipped", detail)."""
    if not path.exists():
        return "missing", "file not found"
    row, error = _sqlite_direct(path)
    if row is not None:
        return _verdict(row)
    status, detail = _sqlite_snapshot(path)
    if status == "broken" and not detail:
        detail = error or "unreadable"
    return status, detail


def fstype_for(path):
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return ""
    best, best_len = "", -1
    try:
        with open("/proc/mounts", "r", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return ""
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = Path(parts[1].replace("\\040", " "))
        if resolved == mount or mount in resolved.parents:
            if len(str(mount)) > best_len:
                best, best_len = parts[2], len(str(mount))
    return best


def running_agents():
    names = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm", "r", errors="replace") as handle:
                    comm = handle.read().strip()
            except OSError:
                continue
            if re.fullmatch(r"(claude|codex)(\.exe)?", comm):
                names.append(f"{comm} (pid {entry})")
    except OSError:
        pass
    return names


def running_agents_summary(limit=4):
    """A short, readable list (this file server runs dozens of agents)."""
    running = running_agents()
    if len(running) <= limit:
        return ", ".join(running)
    return ", ".join(running[:limit]) + f" and {len(running) - limit} more"


def check_report():
    checks = []

    def add(status, title, detail, hint=""):
        checks.append({"status": status, "title": title, "detail": detail, "hint": hint})

    sqlite_home = os.environ.get("CODEX_SQLITE_HOME", "")
    sqlite_home_fs = fstype_for(expand(sqlite_home)) if sqlite_home else ""
    for label, path in (("CODEX_HOME", CODEX_DIR), ("Claude config", CLAUDE_DIR), ("ai-switch home", ROOT)):
        fs_type = fstype_for(path) or "unknown"
        if fs_type in NETWORK_FS:
            moved = path == CODEX_DIR and sqlite_home and sqlite_home_fs not in NETWORK_FS
            detail = f"{path} ({fs_type})"
            if path == CODEX_DIR and sqlite_home:
                detail += f", runtime databases in {sqlite_home} ({sqlite_home_fs or 'unknown'})"
            if moved:
                add("warn", f"{label} is on {fs_type}, runtime databases are not", detail,
                    "Codex keeps state/log/goal databases in CODEX_SQLITE_HOME now; only history.jsonl and "
                    "the rollout files stay on the network share.")
            else:
                hint = ("SQLite databases (Codex runtime state, goals, memories, logs) can be corrupted on network "
                        "filesystems, which makes sessions disappear. Codex only honours the environment variable "
                        "(a sqlite_home config key is ignored):\n"
                        "        export CODEX_SQLITE_HOME=/var/tmp/codex-sqlite-$USER")
                add("warn", f"{label} is on {fs_type}", detail, hint if path == CODEX_DIR else "")
        else:
            add("ok", f"{label} filesystem", f"{path} ({fs_type})")
    if sqlite_home:
        if sqlite_home_fs in NETWORK_FS:
            add("warn", "CODEX_SQLITE_HOME is also on a network filesystem", f"{sqlite_home} ({sqlite_home_fs})",
                "Pick a directory on a local disk, otherwise the runtime databases keep getting corrupted.")
        else:
            add("ok", "CODEX_SQLITE_HOME", f"{sqlite_home} ({sqlite_home_fs or 'unknown'})")

    text = read_text(CODEX) or ""
    persisted = section_value(text, "history", "persistence")
    if persisted and persisted.lower() in ("none", "off", "false"):
        add("fail", "Codex history persistence is disabled",
            f"[history] persistence = \"{persisted}\" in {display_path(CODEX)}",
            "Rollouts are not written, so past sessions cannot be resumed. Remove that setting.")
    else:
        add("ok", "Codex history persistence", persisted or "default (enabled)")

    model = top_level_get(text, "model")
    catalog_path = top_level_get(text, "model_catalog_json")
    live_catalog = parse_json_file(expand(catalog_path)) if catalog_path else parse_json_file(CODEX_MODELS)
    slugs = [e.get("slug") for e in (live_catalog.get("models") or []) if isinstance(e, dict)]
    if catalog_path and not expand(catalog_path).exists():
        add("fail", "Codex model catalogue is missing", f"{catalog_path} does not exist",
            "Re-run 'ai-switch use <profile>' to rewrite it.")
    elif slugs and model and model not in slugs:
        open_ended = False
        try:
            open_ended = bool(json.loads((PROFILES / active_profile() / "models.json").read_text()).get("open_ended"))
        except (OSError, ValueError):
            pass
        detail = f"model = {model}, catalogue has {', '.join(str(s) for s in slugs)}"
        if open_ended:
            add("warn", "Codex model catalogue does not list the configured model", detail,
                "The active profile describes an open-ended endpoint, so any model name is allowed; "
                "'ai-switch use NAME -m MODEL' adds it to the catalogue.")
        else:
            add("fail", "Codex model catalogue does not contain the configured model", detail,
                "Codex refuses to resume sessions whose model is unknown. Re-run 'ai-switch use <profile>'.")
    elif slugs:
        add("ok", "Codex model catalogue", f"{len(slugs)} model(s): {', '.join(str(s) for s in slugs)}")
    else:
        add("ok", "Codex model catalogue", "not used by the current configuration")

    # A gateway that needs the rewrite is only helped while Codex is actually routed
    # through the proxy, so report the two ways that link can be broken.
    name = active_profile()
    wants = patch_needed(read_text(PROFILES / name / "codex-config.toml") or "") if name else None
    proxy = patch_status()
    port = proxy.get("port") or PATCH_PORT
    if wants and _host_without_port(_provider_base_url(text) or "") in ("127.0.0.1", "localhost"):
        if proxy["running"]:
            add("ok", "gateway patch proxy", f"127.0.0.1:{proxy['port']} -> {proxy.get('upstream')}")
        else:
            add("fail", "gateway patch proxy is not running",
                f"{display_path(CODEX)} points at 127.0.0.1:{port}, but nothing answers there",
                f"Restart it with 'ai-switch use {name}'. Until then Codex cannot reach {wants['host']}.")
    elif wants:
        add("warn", "gateway patch proxy is not in the way",
            f"{display_path(CODEX)} still points at {_provider_base_url(text) or 'the provider'}",
            f"{wants['host']} {wants['reason']}, so tool calls fail with \"insufficient tool "
            f"messages following tool_calls\". Run 'ai-switch use {name}' to route Codex through "
            "the proxy.")
    elif proxy["running"]:
        add("warn", "gateway patch proxy is still running",
            f"127.0.0.1:{proxy['port']} -> {proxy.get('upstream')}",
            "The active profile does not need it; the next 'ai-switch use' stops it.")

    databases = sorted(CODEX_DIR.glob("*.sqlite"))
    broken = []
    for db in databases:
        status, detail = sqlite_status(db)
        if status != "ok":
            broken.append((db, detail))
    if broken:
        for db, detail in broken:
            add("fail", f"Codex runtime DB {db.name} is unusable", detail,
                "Codex rebuilds this database from the rollout files when it is moved aside: "
                "'ai-switch doctor --fix', then restart codex.")
    elif databases:
        add("ok", "Codex runtime databases", f"{len(databases)} database(s) passed quick_check")
    else:
        add("note", "Codex runtime databases", "none found")
    for sidecar in sorted(CODEX_DIR.glob("*.sqlite-wal")) + sorted(CODEX_DIR.glob("*.sqlite-shm")):
        if not Path(str(sidecar).rsplit("-", 1)[0]).exists():
            add("warn", f"orphan {sidecar.name}", "main database is gone; it is ignored by Codex")

    sessions = sorted((CODEX_DIR / "sessions").glob("**/*.jsonl")) if (CODEX_DIR / "sessions").exists() else []
    newest = max((s.stat().st_mtime for s in sessions), default=None)
    if sessions:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest))
        add("ok", "Codex rollout files", f"{len(sessions)} rollout(s), newest {when}",
            "Run 'codex resume --all' to list sessions from every directory ('codex resume' filters by cwd).")
    else:
        add("note", "Codex rollout files", "none found", "Sessions start once you use codex.")
    for name in ("history.jsonl", "session_index.jsonl"):
        path = CODEX_DIR / name
        add("ok" if path.exists() else "note", f"Codex {name}",
            f"{display_path(path)} ({path.stat().st_size} bytes)" if path.exists() else "not created yet")

    settings = parse_json_file(CLAUDE)
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    pins = claude_model_pins(settings)
    if env.get("ANTHROPIC_MODEL"):
        add("warn", "Claude Code model is pinned by the environment",
            f"ANTHROPIC_MODEL={env['ANTHROPIC_MODEL']}",
            "A pinned model overrides the settings.json model and disables /model category switching. "
            "Switch with 'ai-switch use <profile>' instead.")
    elif pins:
        distinct = len(set(pins.values()))
        detail = ", ".join(f"{k.split('_')[2]}={v}" for k, v in pins.items())
        add("ok" if distinct > 1 else "warn", "Claude Code model categories", detail,
            "" if distinct > 1 else "Opus/Sonnet/Haiku all map to the same model, so /model has no effect. "
                                    "Re-create the profile with 'ai-switch add' to get distinct mappings.")
    else:
        add("note", "Claude Code model categories", "no provider mappings in settings.json")
    history = CLAUDE_DIR / "history.jsonl"
    projects = CLAUDE_DIR / "projects"
    transcripts = sorted(projects.glob("*/*.jsonl")) if projects.exists() else []
    add("ok" if history.exists() else "note", "Claude prompt history",
        f"{display_path(history)} ({history.stat().st_size} bytes)" if history.exists() else "not created yet")
    add("ok" if transcripts else "note", "Claude transcripts",
        f"{len(transcripts)} transcript(s) under {display_path(projects)}")

    state = load_state()
    active = active_profile()
    recorded = (state.get("models") or {}).get(active) if isinstance(state.get("models"), dict) else None
    add("ok", "Active profile", f"{active or '(none)'}" + (f", model {recorded}" if recorded else ""))
    if recorded:
        # A running agent rewrites its own config on exit, so the live files can
        # silently diverge from what 'ai-switch use' activated.
        if model and model != recorded:
            add("warn", "Codex is not using the model ai-switch activated",
                f"config.toml has model = {model}, ai-switch activated {recorded}",
                "A running codex session rewrites config.toml when it exits, so a /model choice made "
                f"inside codex wins. Close codex and re-run 'ai-switch use {active} -m {recorded}', or use "
                "--pin so the in-app picker cannot switch away.")
        claude_model = str(settings.get("model") or "")
        stem = lambda text: re.split(r"[\[/]", text)[0]
        if claude_model and stem(claude_model) != stem(str(recorded)):
            add("warn", "Claude Code is not using the model ai-switch activated",
                f"settings.json has model = {claude_model}, ai-switch activated {recorded}",
                f"Re-run 'ai-switch use {active} -m {recorded}' to bring both agents back in sync.")
    profiles = [p.name for p in sorted(PROFILES.iterdir()) if p.is_dir()] if PROFILES.exists() else []
    add("ok" if profiles else "note", "Profiles", ", ".join(profiles) or "none yet")

    running = running_agents()
    if running:
        add("warn", "Agents are running", running_agents_summary() + f" ({len(running)} process(es))",
            "A running agent rewrites its configuration on exit and may overwrite the switch; restart it after switching.")
    else:
        add("ok", "No agent processes running", "config.toml / settings.json are not being rewritten")
    return checks


def quarantine_broken_databases(checks, out=print):
    broken = [c for c in checks if c["status"] == "fail" and c["title"].startswith("Codex runtime DB")]
    if not broken:
        out("Nothing to fix: no damaged Codex runtime database found.")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = QUARANTINE / stamp
    target.mkdir(parents=True, exist_ok=True)
    secure(target)
    for check in broken:
        name = check["title"].split("Codex runtime DB ")[1].split(" ")[0]
        base = CODEX_DIR / name
        for path in (base, Path(str(base) + "-wal"), Path(str(base) + "-shm")):
            if path.exists():
                shutil.move(str(path), str(target / path.name))
                out(f"Quarantined {display_path(path)} -> {display_path(target / path.name)}")
    out("\nRestart codex: it rebuilds the runtime database from the rollout files, which brings "
        "resumable sessions back. Rollouts and history.jsonl were not touched.")
    return 0


def cmd_doctor(args):
    checks = check_report()
    if args.json:
        print(json.dumps({"version": VERSION, "checks": checks}, indent=2))
    else:
        symbols = {"ok": "\u2713", "warn": "!", "fail": "\u2717", "note": "-"}
        for check in checks:
            print(f"  {symbols.get(check['status'], '?')} {check['title']}: {check['detail']}")
            if check["hint"]:
                print(f"      -> {check['hint']}")
        failed = sum(1 for c in checks if c["status"] == "fail")
        warned = sum(1 for c in checks if c["status"] == "warn")
        print(f"\n{len(checks) - failed - warned} ok, {warned} warning(s), {failed} failure(s)")
        print("Session history is never part of a profile: ai-switch only manages "
              + ", ".join(PROFILE_FILES) + ".")
    if args.fix:
        if not args.json:
            print()
        quarantine_broken_databases(checks, out=(lambda text="": print(text, file=sys.stderr)) if args.json else print)
    return 1 if any(c["status"] == "fail" for c in checks) else 0


# ---------------------------------------------------------------- entry point
def main(argv=None):
    description = "Switch Claude Code and Codex between model providers in one command."
    epilog = """Examples:
  ai-switch init openai          Save the current configuration as 'openai'
  ai-switch list                 List profiles (* marks the active one)
  ai-switch use glm              Activate 'glm' and choose which model to use
  ai-switch use glm -m glm-5.3   Activate a specific model without prompting
  ai-switch models glm           Show the models a profile offers
  ai-switch upgrade glm          Add a model list to a profile from an older release
  ai-switch doctor               Check configuration and session-history health
  ai-switch doctor --fix         Quarantine damaged Codex runtime databases

Profiles: ~/.config/ai-switch/profiles/    Backups: ~/.config/ai-switch/backups/
Environment: AI_SWITCH_HOME, CODEX_HOME, CLAUDE_CONFIG_DIR, CODEX_SQLITE_HOME

ai-switch never reads or writes session, rollout or history files; only the
files inside a profile are switched. Restart claude/codex after switching."""
    parser = argparse.ArgumentParser(prog="ai-switch", description=description, epilog=epilog,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"ai-switch {VERSION}")
    parser.add_argument("--no-color", action="store_true", help="disable colours and animation everywhere")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("init", help="save the current Codex/Claude config as a new profile")
    p.add_argument("name")
    p.add_argument("-d", "--description", default="")
    p.add_argument("--no-import", action="store_true", help="do not derive a model list from the snapshot")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("list", help="list all profiles")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("use", help="activate a profile (optionally choosing a model)")
    p.add_argument("name")
    p.add_argument("-m", "--model", help="set the default model for new sessions (name, unique prefix, or index)")
    p.add_argument("--choose", action="store_true",
                   help="ask which model the new sessions should start with (the others stay selectable in /model)")
    p.add_argument("--pin", action="store_true",
                   help="publish only this model, so Codex's own picker cannot switch to another one")
    p.add_argument("--plain", action="store_true", help="no progress animation or colours")
    p.add_argument("-y", "--yes", action="store_true", help="accepted for compatibility; use never prompts by default")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would change")
    p.add_argument("--no-check", action="store_true", help="skip the session-history health check")
    p.add_argument("--no-patch", action="store_true",
                   help="do not route Codex through the local gateway patch proxy "
                        "(for endpoints that no longer need it)")
    p.set_defaults(fn=cmd_use)

    p = sub.add_parser("models", help="show the models a profile offers")
    p.add_argument("name", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("current", help="show every profile, its models and the live state")
    p.add_argument("-p", "--plain", action="store_true", help="print only the active profile name")
    p.set_defaults(fn=cmd_current)

    p = sub.add_parser("describe", help="set a profile description")
    p.add_argument("name")
    p.add_argument("text")
    p.set_defaults(fn=cmd_describe)

    p = sub.add_parser("upgrade", help="derive a model list for a profile from an older release")
    p.add_argument("name")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_upgrade)

    p = sub.add_parser("add", help="create a profile interactively (GLM, DeepSeek, or custom)")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("doctor", help="check configuration, model catalogues and session history")
    p.add_argument("--fix", action="store_true", help="quarantine damaged Codex runtime databases")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doctor)

    args = parser.parse_args(argv)
    global COLOR_DISABLED
    COLOR_DISABLED = bool(getattr(args, "no_color", False))
    if not args.cmd:
        parser.print_help()
        print("\nAvailable profiles:")
        cmd_list(args)
        return 0
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was changed.", file=sys.stderr)
        return 130
    except (OSError, ValueError, FileNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
