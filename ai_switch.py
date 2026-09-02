#!/usr/bin/env python3
"""Switch Codex and Claude Code configuration profiles without a GUI."""
import argparse, getpass, json, os, re, shutil, sys, tempfile, time
from pathlib import Path

HOME = Path.home()
CODEX = HOME / ".codex" / "config.toml"
CLAUDE = HOME / ".claude" / "settings.json"
CODEX_AUTH = HOME / ".codex" / "auth.json"
CODEX_MODELS = HOME / ".codex" / "models.json"
ROOT = Path(os.environ.get("AI_SWITCH_HOME", HOME / ".config" / "ai-switch"))
PROFILES = ROOT / "profiles"
STATE = ROOT / "current"

def secure(p):
    try: p.chmod(0o700 if p.is_dir() else 0o600)
    except OSError: pass

def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True); secure(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f: f.write(data)
        os.chmod(tmp, 0o600); os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def profile(name):
    if not name or Path(name).name != name or name in (".", ".."): raise ValueError("invalid profile name")
    return PROFILES / name

def init(name):
    d = profile(name); d.mkdir(parents=True, exist_ok=False); secure(d)
    if CODEX.exists(): shutil.copy2(CODEX, d / "codex-config.toml")
    if CLAUDE.exists(): shutil.copy2(CLAUDE, d / "claude-settings.json")
    if CODEX_AUTH.exists(): shutil.copy2(CODEX_AUTH, d / "codex-auth.json")
    if CODEX_MODELS.exists(): shutil.copy2(CODEX_MODELS, d / "codex-models.json")
    for p in (d / "codex-config.toml", d / "claude-settings.json", d / "codex-auth.json", d / "codex-models.json"): secure(p)
    write_atomic(d / "profile.json", json.dumps({"description": getattr(init, "description", ""), "created": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=True, indent=2) + "\n")
    print(f"Created profile: {name}")

def summary(d):
    meta = {}
    try: meta = json.loads((d / "profile.json").read_text())
    except (OSError, ValueError): pass
    clients=[]; details=[]
    c=d/"codex-config.toml"
    if c.exists():
        clients.append("Codex")
        s=c.read_text(errors="replace")
        model=re.search(r'^model\s*=\s*["\']([^"\']+)',s,re.M)
        base=re.search(r'^base_url\s*=\s*["\']([^"\']+)',s,re.M)
        if model: details.append("Codex model="+model.group(1))
        if base: details.append("Codex endpoint="+base.group(1).split('/')[2] if '://' in base.group(1) else "Codex endpoint configured")
    c=d/"claude-settings.json"
    if c.exists():
        clients.append("Claude")
        try:
            x=json.loads(c.read_text()); env=x.get("env",{}); model=x.get("model")
            if model: details.append("Claude model="+str(model))
            if env.get("ANTHROPIC_BASE_URL"): details.append("Claude endpoint="+str(env["ANTHROPIC_BASE_URL"]).split('/')[2] if '://' in str(env["ANTHROPIC_BASE_URL"]) else "Claude endpoint configured")
        except ValueError: details.append("Claude settings invalid JSON")
    return meta.get("description") or "No description", ",".join(clients) or "No config", "; ".join(details) or "No automatic summary"

def list_profiles(_):
    current = STATE.read_text().strip() if STATE.exists() else ""
    for d in sorted(PROFILES.iterdir() if PROFILES.exists() else []):
        if d.is_dir():
            desc, clients, details = summary(d)
            print(f"{'*' if d.name == current else ' '} {d.name:<16} {clients:<12} {desc} [{details}]")

def use(name):
    d = profile(name)
    if not d.is_dir(): raise FileNotFoundError(f"profile not found: {name}")
    backup = ROOT / "backups" / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True); secure(backup)
    for target, source, label in ((CODEX,d/"codex-config.toml","Codex"),(CLAUDE,d/"claude-settings.json","Claude"),(CODEX_AUTH,d/"codex-auth.json","Codex auth"),(CODEX_MODELS,d/"codex-models.json","Codex models")):
        if source.exists():
            if target.exists(): shutil.copy2(target, backup / target.name); secure(backup / target.name)
            write_atomic(target, source.read_text())
            print(f"Switched {label}")
    write_atomic(STATE, name + "\n")
    print(f"Active profile: {name}\nBackup: {backup}")

def current(_): print(STATE.read_text().strip() if STATE.exists() else "(none)")

def describe(name, text):
    d=profile(name)
    if not d.is_dir(): raise FileNotFoundError(f"profile not found: {name}")
    write_atomic(d/"profile.json", json.dumps({"description": text, "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=True, indent=2)+"\n")
    print(f"Updated description: {name}")

def add_interactive(_):
    print("Create a provider profile (input is not echoed for API keys).")
    print("At any prompt, type 'cancel' or press Ctrl-C to exit without saving.\n")
    provider=input("Provider [glm/deepseek/custom] (default: glm): ").strip().lower() or "glm"
    if provider not in ("glm","deepseek","custom"): raise ValueError("provider must be glm, deepseek, or custom")
    if provider == "glm":
        endpoint="https://open.bigmodel.cn/api/v1"; model="glm-5.3"; claude_endpoint="https://open.bigmodel.cn/api/anthropic"
        print("Using GLM preset: Codex Responses API and Claude model mappings will be configured automatically.")
    elif provider == "deepseek":
        endpoint="https://api.deepseek.com/"; model="deepseek-v4-flash"; claude_endpoint="https://api.deepseek.com/anthropic"
        print("Using DeepSeek preset: Codex Responses API, model catalog, and Claude model mappings will be configured automatically.")
    else:
        endpoint=input("API endpoint URL (e.g. https://api.example.com/v1): ").strip()
        model=input("Model name: ").strip(); claude_endpoint=endpoint[:-3].rstrip("/") if endpoint.endswith("/v1") else endpoint.rstrip("/")
    name=input("Profile name: ").strip(); desc=input("Description: ").strip()
    key=getpass.getpass("API key: ")
    clients=input("Configure clients [both/codex/claude] (default: both): ").strip().lower() or "both"
    if clients not in ("both","codex","claude"): raise ValueError("clients must be both, codex, or claude")
    d=profile(name); d.mkdir(parents=True, exist_ok=False); secure(d)
    if clients in ("both","codex"):
        if provider == "glm":
            base='model_provider = "ZAI"\nmodel = "glm-5.3"\nmodel_reasoning_effort = "max"\nmodel_catalog_json = "~/.codex/models.json"\n\n[model_providers.ZAI]\nname = "ZAI"\nbase_url = "https://open.bigmodel.cn/api/v1"\nexperimental_bearer_token = "'+key+'"\nwire_api = "responses"\n'
        elif provider == "deepseek":
            base='model = "deepseek-v4-flash"\nmodel_provider = "deepseek"\npreferred_auth_method = "apikey"\nforced_login_method = "api"\nmodel_reasoning_effort = "high"\nmodel_catalog_json = "~/.codex/models.json"\n\n[model_providers.deepseek]\nname = "deepseek"\nbase_url = "https://api.deepseek.com/"\nwire_api = "responses"\nexperimental_bearer_token = "'+key+'"\n'
        else: base=CODEX.read_text() if CODEX.exists() else 'model_provider = "custom"\nmodel = "MODEL"\n\n[model_providers.custom]\nname = "custom"\nbase_url = "ENDPOINT"\nwire_api = "responses"\nrequires_openai_auth = true\n'
        base=re.sub(r'(?m)^model\s*=\s*["\'][^"\']*["\']', f'model = "{model}"', base, count=1)
        base=re.sub(r'(?m)^base_url\s*=\s*["\'][^"\']*["\']', f'base_url = "{endpoint}"', base, count=1)
        write_atomic(d/"codex-config.toml", base)
        write_atomic(d/"codex-auth.json", json.dumps({"OPENAI_API_KEY":key}, indent=2)+"\n")
        if provider in ("glm", "deepseek"):
            if provider == "deepseek":
                models=[]
                for slug, desc, modalities, priority, context in (("deepseek-v4-flash","Fast general-purpose DeepSeek model",["text"],0,1048576),("deepseek-v4-pro","Deep reasoning DeepSeek model",["text"],1,1048576),("deepseek-v4-flash-vision-exp","DeepSeek vision model",["text","image"],2,1048576)):
                    models.append({"slug":slug,"display_name":slug,"description":desc,"default_reasoning_level":"high","supported_reasoning_levels":[{"effort":"low","description":"Light reasoning"},{"effort":"high","description":"Enhanced reasoning"},{"effort":"max","description":"Deep reasoning"}],"shell_type":"shell_command","visibility":"list","supported_in_api":True,"priority":priority,"base_instructions":"","supports_reasoning_summaries":True,"default_reasoning_summary":"none","support_verbosity":False,"apply_patch_tool_type":"freeform","truncation_policy":{"mode":"bytes","limit":10000},"context_window":context,"max_context_window":context,"effective_context_window_percent":95,"supports_parallel_tool_calls":True,"experimental_supported_tools":[],"input_modalities":modalities})
                write_atomic(d/"codex-models.json", json.dumps({"models":models}, indent=2)+"\n")
            else:
                catalog={"models":[{"slug":"glm-5.3","display_name":"glm-5.3","description":"Z.ai flagship model","default_reasoning_level":"max","supported_reasoning_levels":[{"effort":"low","description":"Light reasoning"},{"effort":"high","description":"Enhanced reasoning"},{"effort":"max","description":"Deep reasoning"}],"shell_type":"shell_command","visibility":"list","supported_in_api":True,"priority":0,"base_instructions":"","supports_reasoning_summaries":True,"default_reasoning_summary":"none","support_verbosity":False,"apply_patch_tool_type":"freeform","truncation_policy":{"mode":"bytes","limit":10000},"context_window":1048576,"max_context_window":1048576,"effective_context_window_percent":95,"supports_parallel_tool_calls":True,"experimental_supported_tools":[],"input_modalities":["text"]}]}
                write_atomic(d/"codex-models.json", json.dumps(catalog, indent=2)+"\n")
    if clients in ("both","claude"):
        obj=json.loads(CLAUDE.read_text()) if CLAUDE.exists() else {}
        obj.setdefault("env",{}).update({"ANTHROPIC_BASE_URL":claude_endpoint,"ANTHROPIC_AUTH_TOKEN":key})
        if provider == "glm": obj["env"].update({"ANTHROPIC_DEFAULT_HAIKU_MODEL":"glm-5.3-flash[1m]","ANTHROPIC_DEFAULT_SONNET_MODEL":"glm-5.3[1m]","ANTHROPIC_DEFAULT_OPUS_MODEL":"glm-5.3[1m]","CLAUDE_CODE_AUTO_COMPACT_WINDOW":"1000000","CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":1,"API_TIMEOUT_MS":"3000000"})
        elif provider == "deepseek": obj["env"].update({"ANTHROPIC_MODEL":"deepseek-v4-pro[1m]","ANTHROPIC_DEFAULT_OPUS_MODEL":"deepseek-v4-pro[1m]","ANTHROPIC_DEFAULT_SONNET_MODEL":"deepseek-v4-pro[1m]","ANTHROPIC_DEFAULT_HAIKU_MODEL":"deepseek-v4-flash","CLAUDE_CODE_SUBAGENT_MODEL":"deepseek-v4-flash","CLAUDE_CODE_EFFORT_LEVEL":"max","CLAUDE_CODE_AUTO_COMPACT_WINDOW":"786432"})
        obj["model"]=model; write_atomic(d/"claude-settings.json", json.dumps(obj, indent=2)+"\n")
    write_atomic(d/"profile.json", json.dumps({"description":desc,"created":time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2)+"\n")
    print(f"Created profile: {name}. Activate it with: ai-switch use {name}")

def main():
    description = "Switch Codex and Claude Code API profiles without a GUI."
    epilog = """Examples:
  ai-switch init openai       Save the current configuration as 'openai'
  ai-switch list              List profiles (* marks the active one)
  ai-switch use glm           Activate the 'glm' profile
  ai-switch current           Show the active profile

Profiles: ~/.config/ai-switch/profiles/
Backups:  ~/.config/ai-switch/backups/
Set AI_SWITCH_HOME to override the storage directory.
After switching, restart claude/codex so they reload their configuration."""
    ap=argparse.ArgumentParser(prog="ai-switch", description=description,
                               epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp=ap.add_subparsers(dest="cmd")
    p=sp.add_parser("init", help="save current Codex/Claude config as a new profile", description="Save the current configuration files as a new profile.")
    p.add_argument("name", help="profile name (letters, numbers, . _ -; no path separators)")
    p.add_argument("-d", "--description", default="", help="human-readable purpose, e.g. 'GLM Coding Plan'")
    p.set_defaults(fn=init)
    p=sp.add_parser("list", help="list all profiles", description="List profiles; '*' marks the active profile."); p.set_defaults(fn=list_profiles)
    p=sp.add_parser("use", help="activate a profile", description="Back up current files and atomically activate the selected profile.")
    p.add_argument("name", help="profile name"); p.set_defaults(fn=use)
    p=sp.add_parser("current", help="show active profile", description="Print the active profile name, or '(none)'."); p.set_defaults(fn=current)
    p=sp.add_parser("describe", help="set a profile description", description="Update the human-readable description of an existing profile.")
    p.add_argument("name", help="profile name"); p.add_argument("text", help="description"); p.set_defaults(fn=None)
    p=sp.add_parser("add", help="create a profile interactively", description="Interactively create a profile without editing configuration files."); p.set_defaults(fn=add_interactive)
    a=ap.parse_args()
    if not a.cmd:
        ap.print_help()
        print("\nAvailable profiles:")
        list_profiles(a)
        return 0
    if a.cmd == "init": init.description = a.description
    if a.cmd == "describe": a.fn = lambda n: describe(n, a.text)
    try: a.fn(getattr(a,"name",a))
    except KeyboardInterrupt:
        print("\nCancelled. No profile was created.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as e: print(f"Error: {e}", file=sys.stderr); return 1
    return 0
if __name__ == "__main__": raise SystemExit(main())
