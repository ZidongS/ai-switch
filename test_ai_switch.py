import contextlib, io, json, os, signal, socket, sqlite3, sys, tempfile, threading, time, unittest
import urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ai_switch


def sandbox(case):
    """Redirect every path ai_switch uses into a throw-away home directory."""
    manager = tempfile.TemporaryDirectory()
    case.addCleanup(manager.cleanup)
    base = Path(manager.name)
    home, root = base / "home", base / "root"
    codex_dir, claude_dir = home / ".codex", home / ".claude"
    codex_dir.mkdir(parents=True)
    claude_dir.mkdir(parents=True)
    original_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)  # "~" in a profile must never escape the sandbox
    case.addCleanup(lambda: os.environ.__setitem__("HOME", original_home) if original_home else
                    os.environ.pop("HOME", None))
    values = {
        "HOME": home, "CODEX_DIR": codex_dir, "CLAUDE_DIR": claude_dir,
        "CODEX": codex_dir / "config.toml", "CLAUDE": claude_dir / "settings.json",
        "CODEX_AUTH": codex_dir / "auth.json", "CODEX_MODELS": codex_dir / "models.json",
        "ROOT": root, "PROFILES": root / "profiles", "CURRENT": root / "current",
        "STATE": root / "state.json", "QUARANTINE": root / "quarantine",
    }
    original = {name: getattr(ai_switch, name) for name in values}
    for name, value in values.items():
        setattr(ai_switch, name, value)
    case.addCleanup(lambda: [setattr(ai_switch, n, v) for n, v in original.items()])
    return values


def run(case, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ai_switch.main(list(argv))
    case.out, case.err = out.getvalue(), err.getvalue()
    return code


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))


CODEX_CONFIG = 'model = "old-model"\nmodel_provider = "p"\nmodel_catalog_json = "~/.codex/models.json"\n\n[model_providers.p]\nname = "p"\nbase_url = "https://old.example/v1"\n'

MODELS = {"version": 2, "provider": "test", "default": "m-one",
          "models": [
              {"slug": "m-one", "label": "first", "reasoning": "high",
               "codex": {"slug": "m-one", "description": "first"},
               "claude": {"model": "m-one[1m]", "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "m-one[1m]"}}},
              {"slug": "m-two", "label": "second", "reasoning": "max",
               "codex": {"slug": "m-two", "description": "second"},
               "claude": {"model": "m-two", "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "m-two",
                                                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "m-two-mini"}}}]}


class ProfileNameTest(unittest.TestCase):
    def test_profile_name(self):
        with self.assertRaises(ValueError):
            ai_switch.profile("../bad")
        with self.assertRaises(ValueError):
            ai_switch.profile("")

    def test_atomic(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "x"
            ai_switch.write_atomic(path, "secret")
            self.assertEqual(path.read_text(), "secret")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class TomlTest(unittest.TestCase):
    def test_top_level_only(self):
        text = 'model = "a"\n\n[model_providers.x]\nmodel = "inner"\nbase_url = "https://h/v1"\n'
        self.assertEqual(ai_switch.top_level_get(text, "model"), "a")
        updated = ai_switch.top_level_set(text, "model", "b")
        self.assertIn('model = "b"', updated)
        self.assertIn('model = "inner"', updated)

    def test_create_missing_key_before_sections(self):
        text = 'model = "a"\n\n[s]\nk = 1\n'
        updated = ai_switch.top_level_set(text, "model_catalog_json", "~/.codex/models.json")
        self.assertEqual(ai_switch.top_level_get(updated, "model_catalog_json"), "~/.codex/models.json")
        self.assertLess(updated.index("model_catalog_json"), updated.index("[s]"))

    def test_section_value_and_drop(self):
        text = 'model = "a"\n\n[history]\npersistence = "none"\n'
        self.assertEqual(ai_switch.section_value(text, "history", "persistence"), "none")
        self.assertEqual(ai_switch.section_value(text, "other", "persistence"), None)
        self.assertNotIn("model_catalog_json", ai_switch.top_level_drop(text, "model"))


class ClaudeMergeTest(unittest.TestCase):
    def test_agent_state_is_preserved_and_provider_env_replaced(self):
        live = {"env": {"ANTHROPIC_BASE_URL": "https://old", "ANTHROPIC_AUTH_TOKEN": "old",
                        "ANTHROPIC_MODEL": "old-model", "MY_OWN_VAR": "1"},
                "modelSettings": {"old-model": {"effortLevel": "low"}},
                "availableModels": ["claude-opus-5"], "hasCompletedOnboarding": True}
        profile = {"env": {"ANTHROPIC_BASE_URL": "https://new", "ANTHROPIC_AUTH_TOKEN": "new",
                           "ANTHROPIC_MODEL": "pinned", "CLAUDE_CODE_EFFORT_LEVEL": "max"},
                   "model": "profile-model", "skipDangerousModePermissionPrompt": True}
        entry = {"slug": "s", "claude": {"model": "s[1m]", "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "s[1m]"}}}
        merged = ai_switch.build_claude_settings(live, profile, entry)
        self.assertEqual(merged["model"], "s[1m]")
        self.assertEqual(merged["env"]["ANTHROPIC_BASE_URL"], "https://new")
        self.assertEqual(merged["env"]["ANTHROPIC_AUTH_TOKEN"], "new")
        self.assertEqual(merged["env"]["MY_OWN_VAR"], "1")
        self.assertEqual(merged["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "s[1m]")
        self.assertNotIn("ANTHROPIC_MODEL", merged["env"])          # no pin: /model stays selectable
        self.assertEqual(merged["modelSettings"], {"old-model": {"effortLevel": "low"}})
        self.assertEqual(merged["availableModels"], ["claude-opus-5"])
        self.assertTrue(merged["hasCompletedOnboarding"])
        self.assertTrue(merged["skipDangerousModePermissionPrompt"])

    def test_derive_drops_pin_and_substitutes_default_model(self):
        settings = {"model": "flash", "env": {"ANTHROPIC_MODEL": "pro[1m]",
                                              "ANTHROPIC_DEFAULT_OPUS_MODEL": "pro[1m]",
                                              "ANTHROPIC_DEFAULT_HAIKU_MODEL": "flash"}}
        derived = ai_switch.derive_claude_mapping(settings, "flash", "flash-mini")
        self.assertNotIn("ANTHROPIC_MODEL", derived["env"])
        self.assertEqual(derived["model"], "flash-mini")
        self.assertEqual(derived["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "pro[1m]")
        self.assertEqual(derived["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "flash-mini")


class PresetTest(unittest.TestCase):
    def test_presets_offer_several_models(self):
        self.assertGreaterEqual(len(ai_switch.PRESETS["glm"]["models"]), 2)
        self.assertGreaterEqual(len(ai_switch.PRESETS["deepseek"]["models"]), 2)

    def test_glm_preset_uses_names_the_zai_endpoint_accepts(self):
        slugs = [m["slug"] for m in ai_switch.PRESETS["glm"]["models"]]
        self.assertEqual(slugs, ["glm-5.3", "glm-5.3-flash", "glm-5-turbo"])
        for entry in ai_switch.PRESETS["glm"]["models"]:
            self.assertEqual(entry["claude"]["model"], entry["slug"])
            for value in entry["claude"]["env"].values():
                self.assertNotIn("[1m]", str(value), entry["slug"])
        turbo = ai_switch.PRESETS["glm"]["models"][2]
        self.assertEqual(turbo["claude"]["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "190000")
        self.assertEqual(turbo["codex"]["context_window"], 204800)

    def test_deepseek_preset_uses_current_api_model_names(self):
        slugs = [m["slug"] for m in ai_switch.PRESETS["deepseek"]["models"]]
        self.assertEqual(slugs, ["deepseek-flash", "deepseek-v4-pro"])
        self.assertEqual(ai_switch.PRESETS["deepseek"]["default"], "deepseek-flash")

    def test_presets_map_claude_categories_distinctly(self):
        for name, preset in ai_switch.PRESETS.items():
            entry = ai_switch.PRESETS[name]["models"][0]
            env = entry["claude"]["env"]
            self.assertIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env, name)
            self.assertIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", env, name)
            self.assertNotIn("ANTHROPIC_MODEL", env, name)  # never pin the main model
            self.assertNotEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], name)


class UseTest(unittest.TestCase):
    def setUp(self):
        self.paths = sandbox(self)
        self.profile = self.paths["PROFILES"] / "p"
        write(self.profile / "codex-config.toml", CODEX_CONFIG)
        write(self.profile / "models.json", MODELS)
        write(self.profile / "claude-settings.json",
              {"env": {"ANTHROPIC_BASE_URL": "https://new", "ANTHROPIC_AUTH_TOKEN": "tok"}})
        write(self.paths["CODEX"], CODEX_CONFIG)
        write(self.paths["CLAUDE"], {"env": {"ANTHROPIC_MODEL": "stale", "KEEP": "yes"}, "modelSettings": {"x": 1}})
        self.history = self.paths["CODEX_DIR"] / "history.jsonl"
        write(self.history, '{"text":"keep me"}\n')
        self.sessions = self.paths["CODEX_DIR"] / "sessions" / "2026" / "01" / "01" / "r.jsonl"
        write(self.sessions, '{"type":"session"}\n')
        write(self.paths["CLAUDE_DIR"] / "history.jsonl", '{"display":"keep"}\n')

    def test_use_default_model_writes_both_clients(self):
        self.assertEqual(run(self, "use", "p", "--yes"), 0)
        config = self.paths["CODEX"].read_text()
        self.assertIn('model = "m-one"', config)
        self.assertIn('base_url = "https://old.example/v1"', config)   # snapshot restored
        catalog = json.loads(self.paths["CODEX_MODELS"].read_text())
        self.assertEqual([m["slug"] for m in catalog["models"]], ["m-one", "m-two"])
        settings = json.loads(self.paths["CLAUDE"].read_text())
        self.assertEqual(settings["model"], "m-one[1m]")
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "https://new")
        self.assertEqual(settings["env"]["KEEP"], "yes")               # non-provider env survives
        self.assertNotIn("ANTHROPIC_MODEL", settings["env"])
        self.assertEqual(settings["modelSettings"], {"x": 1})
        self.assertEqual(settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "m-one[1m]")

    def test_use_specific_model_and_reasoning_effort(self):
        self.assertEqual(run(self, "use", "p", "-m", "m-two"), 0)
        config = self.paths["CODEX"].read_text()
        self.assertIn('model = "m-two"', config)
        self.assertIn('model_reasoning_effort = "max"', config)
        settings = json.loads(self.paths["CLAUDE"].read_text())
        self.assertEqual(settings["model"], "m-two")
        self.assertEqual(settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "m-two-mini")

    def test_model_can_be_selected_by_index_and_prefix(self):
        self.assertEqual(run(self, "use", "p", "-m", "2"), 0)
        self.assertIn('model = "m-two"', self.paths["CODEX"].read_text())
        self.assertEqual(run(self, "use", "p", "-m", "m-o"), 0)
        self.assertIn('model = "m-one"', self.paths["CODEX"].read_text())

    def test_unknown_model_is_an_error(self):
        self.assertEqual(run(self, "use", "p", "-m", "nope"), 1)
        self.assertIn("unknown model", self.err)

    def test_remembered_model_is_reused(self):
        self.assertEqual(run(self, "use", "p", "-m", "m-two"), 0)
        self.assertEqual(run(self, "use", "p", "--yes"), 0)
        self.assertIn('model = "m-two"', self.paths["CODEX"].read_text())

    def test_history_files_are_never_touched(self):
        before = {p: p.read_text() for p in (self.history, self.sessions,
                                             self.paths["CLAUDE_DIR"] / "history.jsonl")}
        self.assertEqual(run(self, "use", "p", "-m", "m-two"), 0)
        for path, text in before.items():
            self.assertEqual(path.read_text(), text)
        self.assertTrue(ai_switch.is_history_path(self.history))
        self.assertTrue(ai_switch.is_history_path(self.sessions))
        self.assertFalse(ai_switch.is_history_path(self.paths["CODEX"]))

    def test_refuses_to_write_a_history_path(self):
        with self.assertRaises(ValueError):
            ai_switch.apply_changes({self.history: "clobbered"}, "p", None)
        self.assertEqual(self.history.read_text(), '{"text":"keep me"}\n')

    def test_backup_is_written_before_overwriting(self):
        self.assertEqual(run(self, "use", "p", "--yes"), 0)
        backups = sorted(self.paths["ROOT"].glob("backups/*"))
        self.assertTrue(backups)
        names = {p.name for p in backups[-1].iterdir()}
        self.assertIn("config.toml", names)
        self.assertIn("settings.json", names)

    def test_use_does_not_prompt_and_keeps_the_whole_menu(self):
        original = sys.stdin
        sys.stdin = io.StringIO("")          # a prompt would hit EOF and fail the test
        try:
            self.assertEqual(run(self, "use", "p"), 0)
        finally:
            sys.stdin = original
        self.assertIn("Default model: m-one", self.out)
        self.assertIn("Published models: 2", self.out)
        catalog = json.loads(self.paths["CODEX_MODELS"].read_text())
        self.assertEqual([m["slug"] for m in catalog["models"]], ["m-one", "m-two"])

    def test_choose_asks_which_model_new_sessions_start_with(self):
        original = sys.stdin
        sys.stdin = io.StringIO("2\n")
        try:
            self.assertEqual(run(self, "use", "p", "--choose"), 0)
        finally:
            sys.stdin = original
        self.assertIn('model = "m-two"', self.paths["CODEX"].read_text())
        catalog = json.loads(self.paths["CODEX_MODELS"].read_text())
        self.assertEqual(len(catalog["models"]), 2)   # choosing does not hide the others

    def test_pin_publishes_only_the_activated_model(self):
        self.assertEqual(run(self, "use", "p", "-m", "m-two", "--pin"), 0)
        catalog = json.loads(self.paths["CODEX_MODELS"].read_text())
        self.assertEqual([m["slug"] for m in catalog["models"]], ["m-two"])
        self.assertIn('model = "m-two"', self.paths["CODEX"].read_text())
        self.assertIn("Pinned", self.out)
        # without --pin the whole menu comes back
        self.assertEqual(run(self, "use", "p", "-m", "m-one"), 0)
        catalog = json.loads(self.paths["CODEX_MODELS"].read_text())
        self.assertEqual([m["slug"] for m in catalog["models"]], ["m-one", "m-two"])

    def test_dry_run_changes_nothing(self):
        original = self.paths["CODEX"].read_text()
        self.assertEqual(run(self, "use", "p", "-m", "m-two", "--dry-run"), 0)
        self.assertEqual(self.paths["CODEX"].read_text(), original)
        self.assertFalse(self.paths["CURRENT"].exists())


class LegacyProfileTest(unittest.TestCase):
    def setUp(self):
        self.paths = sandbox(self)
        self.profile = self.paths["PROFILES"] / "legacy"
        write(self.profile / "codex-config.toml", CODEX_CONFIG)
        write(self.profile / "codex-models.json",
              {"models": [{"slug": "m-one", "description": "one"}, {"slug": "m-two", "description": "two"}]})
        write(self.profile / "claude-settings.json",
              {"model": "m-one", "env": {"ANTHROPIC_BASE_URL": "https://new", "ANTHROPIC_MODEL": "m-one",
                                         "ANTHROPIC_DEFAULT_HAIKU_MODEL": "m-one-mini"}})
        self.plain = self.paths["PROFILES"] / "plain"        # a profile without any catalogue
        write(self.plain / "codex-config.toml", 'model = "hand-picked"\nmodel_provider = "plain"\n')
        write(self.plain / "claude-settings.json", {"env": {"ANTHROPIC_BASE_URL": "https://plain"}})
        write(self.paths["CODEX"], CODEX_CONFIG)
        write(self.paths["CODEX_MODELS"], {"models": [{"slug": "m-one"}]})
        write(self.paths["CLAUDE"], {})
        ai_switch.save_state({"profile": "other", "models": {},
                              "files": {str(self.paths["CODEX_MODELS"]): ai_switch.digest(
                                  self.paths["CODEX_MODELS"].read_text())}})

    def test_models_are_derived_from_the_old_catalogue(self):
        self.assertEqual(run(self, "models", "legacy"), 0)
        self.assertIn("m-two", self.out)
        self.assertIn("2 model(s)", self.out)

    def test_upgrade_writes_models_json(self):
        self.assertEqual(run(self, "upgrade", "legacy"), 0)
        catalog = json.loads((self.profile / "models.json").read_text())
        self.assertEqual([m["slug"] for m in catalog["models"]], ["m-one", "m-two"])
        self.assertEqual(ai_switch.load_catalog(self.profile)["default"], "m-one")

    def test_stale_catalogue_from_another_provider_is_removed(self):
        self.assertEqual(run(self, "use", "plain", "--yes"), 0)
        self.assertFalse(self.paths["CODEX_MODELS"].exists())
        self.assertIn("stale", self.err)
        self.assertNotIn("model_catalog_json", self.paths["CODEX"].read_text())
        self.assertEqual(self.paths["CODEX"].read_text().count('model = "hand-picked"'), 1)

    def test_hand_edited_catalogue_is_left_alone(self):
        hand_written = json.dumps({"models": [{"slug": "hand-written"}]})
        write(self.paths["CODEX_MODELS"], hand_written)
        self.assertEqual(run(self, "use", "plain", "--yes"), 0)
        self.assertEqual(self.paths["CODEX_MODELS"].read_text(), hand_written)
        self.assertIn("modified by hand", self.err)

    def test_open_ended_profile_publishes_listed_models_and_accepts_new_ones(self):
        open_ended = self.paths["PROFILES"] / "custom"
        write(open_ended / "codex-config.toml", 'model = "x"\nmodel_provider = "custom"\n')
        write(open_ended / "models.json", {"version": 2, "open_ended": True, "default": "x",
                                           "models": [{"slug": "x", "codex": {"slug": "x"}}]})
        write(open_ended / "claude-settings.json", {"env": {"ANTHROPIC_BASE_URL": "https://open-ended"}})
        self.assertEqual(run(self, "use", "custom", "--yes"), 0)
        catalog = json.loads(self.paths["CODEX_MODELS"].read_text())
        self.assertEqual([m["slug"] for m in catalog["models"]], ["x"])
        self.assertEqual(ai_switch.top_level_get(self.paths["CODEX"].read_text(), "model_catalog_json"),
                         "~/.codex/models.json")
        # a model outside the list is accepted and added to the catalogue
        self.assertEqual(run(self, "use", "custom", "-m", "any-other-model"), 0)
        self.assertIn('model = "any-other-model"', self.paths["CODEX"].read_text())
        catalog = json.loads(self.paths["CODEX_MODELS"].read_text())
        self.assertEqual([m["slug"] for m in catalog["models"]], ["x", "any-other-model"])
        settings = json.loads(self.paths["CLAUDE"].read_text())
        self.assertEqual(settings["model"], "any-other-model")
        self.assertEqual(settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "any-other-model")
        self.assertEqual(settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "any-other-model")


class AddCommandTest(unittest.TestCase):
    """One API key + URL must be able to carry several models."""

    def setUp(self):
        self.paths = sandbox(self)

    def _add(self, answers):
        original = sys.stdin
        sys.stdin = io.StringIO("\n".join(answers) + "\n")
        try:
            return run(self, "add")
        finally:
            sys.stdin = original

    def test_custom_provider_accepts_several_models(self):
        code = self._add(["custom", "sk-test", "https://gw.example/v1", "Big-Model Small-Model",
                          "Small-Model", "gw", "gateway", "both"])
        self.assertEqual(code, 0)
        profile = self.paths["PROFILES"] / "gw"
        catalog = json.loads((profile / "models.json").read_text())
        self.assertEqual([m["slug"] for m in catalog["models"]], ["Big-Model", "Small-Model"])
        self.assertEqual(catalog["default"], "Small-Model")
        self.assertTrue(catalog["open_ended"])
        published = json.loads((profile / "codex-models.json").read_text())
        self.assertEqual([m["slug"] for m in published["models"]], ["Big-Model", "Small-Model"])
        config = (profile / "codex-config.toml").read_text()
        self.assertIn('model = "Small-Model"', config)
        self.assertIn('base_url = "https://gw.example/v1"', config)
        self.assertIn("model_catalog_json", config)
        settings = json.loads((profile / "claude-settings.json").read_text())
        self.assertEqual(settings["model"], "Small-Model")
        self.assertEqual(settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "Small-Model")
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "https://gw.example")  # Claude Code adds /v1/messages
        # switching to the other model moves both agents
        self.assertEqual(run(self, "use", "gw", "-m", "Big-Model"), 0)
        self.assertIn('model = "Big-Model"', self.paths["CODEX"].read_text())
        settings = json.loads(self.paths["CLAUDE"].read_text())
        self.assertEqual(settings["model"], "Big-Model")
        self.assertEqual(settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "Big-Model")
        self.assertEqual(settings["env"]["ANTHROPIC_AUTH_TOKEN"], "sk-test")

    def test_comma_separated_and_preset_subset_still_work(self):
        code = self._add(["glm", "sk-test", "glm-5.3,glm-5-turbo", "glm-5-turbo", "glm2", "", "both"])
        self.assertEqual(code, 0)
        catalog = json.loads((self.paths["PROFILES"] / "glm2" / "models.json").read_text())
        self.assertEqual([m["slug"] for m in catalog["models"]], ["glm-5.3", "glm-5-turbo"])
        self.assertEqual(catalog["default"], "glm-5-turbo")

    def test_a_single_model_still_works(self):
        code = self._add(["custom", "sk-test", "https://gw.example/v1", "One-Model", "one", "", "codex"])
        self.assertEqual(code, 0)
        catalog = json.loads((self.paths["PROFILES"] / "one" / "models.json").read_text())
        self.assertEqual([m["slug"] for m in catalog["models"]], ["One-Model"])
        self.assertFalse((self.paths["PROFILES"] / "one" / "claude-settings.json").exists())


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.paths = sandbox(self)
        write(self.paths["CODEX"], CODEX_CONFIG)
        write(self.paths["CODEX_MODELS"], {"models": [{"slug": "old-model"}]})
        write(self.paths["CODEX_DIR"] / "history.jsonl", '{"ts":1}\n')

    def test_healthy_setup_reports_ok(self):
        self.assertEqual(run(self, "doctor"), 0)
        self.assertIn("0 failure(s)", self.out)

    def test_broken_and_missing_databases_are_reported(self):
        write(self.paths["CODEX_DIR"] / "state_5.sqlite", "definitely not a database")
        self.assertEqual(run(self, "doctor", "--json"), 1)
        report = json.loads(self.out)
        failures = [c for c in report["checks"] if c["status"] == "fail"]
        self.assertTrue(any("state_5.sqlite" in c["title"] for c in failures))

    def test_malformed_sqlite_file_is_detected(self):
        path = self.paths["CODEX_DIR"] / "logs_2.sqlite"
        path.write_bytes(b"SQLite format 3\x00" + os.urandom(5000))
        status, detail = ai_switch.sqlite_status(path)
        self.assertEqual(status, "broken")
        self.assertTrue(detail)
        self.assertEqual(run(self, "doctor"), 1)
        self.assertIn("logs_2.sqlite", self.out)

    def test_fix_quarantines_broken_databases_and_keeps_history(self):
        write(self.paths["CODEX_DIR"] / "thread_history_1.sqlite", "not a database")
        self.assertEqual(run(self, "doctor", "--fix"), 1)
        self.assertFalse((self.paths["CODEX_DIR"] / "thread_history_1.sqlite").exists())
        quarantined = list(self.paths["QUARANTINE"].glob("*/thread_history_1.sqlite"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual((self.paths["CODEX_DIR"] / "history.jsonl").read_text(), '{"ts":1}\n')

    def test_history_persistence_disabled_is_a_failure(self):
        write(self.paths["CODEX"], CODEX_CONFIG + '\n[history]\npersistence = "none"\n')
        self.assertEqual(run(self, "doctor"), 1)
        self.assertIn("history persistence is disabled", self.out)

    def test_missing_catalogue_entry_for_configured_model_is_a_failure(self):
        write(self.paths["CODEX"], 'model = "ghost"\nmodel_catalog_json = "~/.codex/models.json"\n')
        self.assertEqual(run(self, "doctor"), 1)
        self.assertIn("does not contain the configured model", self.out)

    def test_drift_between_ai_switch_state_and_codex_config_is_reported(self):
        write(self.paths["CODEX"], 'model = "other-model"\nmodel_catalog_json = "~/.codex/models.json"\n')
        write(self.paths["PROFILES"] / "p" / "models.json",
              {"version": 2, "default": "m", "models": [{"slug": "m", "codex": {"slug": "m"}}]})
        ai_switch.save_state({"profile": "p", "models": {"p": "m"}, "files": {}})
        write(self.paths["CURRENT"], "p\n")
        run(self, "doctor")
        self.assertIn("not using the model ai-switch activated", self.out)
        self.assertIn("re-run 'ai-switch use p -m m'", self.out.lower())

    def test_pinned_claude_model_is_reported(self):
        write(self.paths["CLAUDE"], {"env": {"ANTHROPIC_MODEL": "pinned", "ANTHROPIC_BASE_URL": "https://x"}})
        run(self, "doctor")
        self.assertIn("pinned", self.out)


class PresentationTest(unittest.TestCase):
    def setUp(self):
        self.paths = sandbox(self)

    def _tty_style(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        stream = Tty()
        import os as _os
        saved = _os.environ.get("NO_COLOR"), _os.environ.get("TERM")
        _os.environ["NO_COLOR"] = ""
        _os.environ["TERM"] = "xterm-256color"
        self.addCleanup(lambda: (_os.environ.__setitem__("NO_COLOR", saved[0]) if saved[0] is not None
                                 else _os.environ.pop("NO_COLOR", None),
                                 _os.environ.__setitem__("TERM", saved[1]) if saved[1] is not None
                                 else _os.environ.pop("TERM", None)))
        return ai_switch.Style(stream=stream), stream

    def test_progress_falls_back_to_plain_lines_without_a_terminal(self):
        stream = io.StringIO()
        style = ai_switch.Style(stream=stream)               # not a tty
        progress = ai_switch.Progress(2, style, stream=stream)
        self.assertFalse(progress.enabled)
        progress.step("write ~/.codex/models.json", "31 model(s)")
        self.assertIn("write ~/.codex/models.json: 31 model(s)", stream.getvalue())
        self.assertNotIn("█", stream.getvalue())

    def test_progress_draws_spinner_bar_and_result_card_on_a_terminal(self):
        style, stream = self._tty_style()
        progress = ai_switch.Progress(2, style, stream=stream)
        self.assertTrue(progress.enabled)
        progress.animate("activating p", frames=2, delay=0)
        progress.step("write ~/.codex/config.toml", "model m-one")
        progress.sweep("activation complete", frames=3, delay=0)
        progress.result("p is active", [("profile", "p"), ("models", "2 published", "2 published")])
        text = stream.getvalue()
        self.assertIn("activating p", text)
        self.assertIn("✓", text)
        self.assertIn("▰", text)          # the bar fills with a gradient
        self.assertIn("▱", text)
        self.assertIn("100%", text)       # …and always ends completely full
        self.assertIn("p is active", text)
        self.assertIn("2 published", text)
        self.assertIn("╭", text)          # rounded result card

    def test_current_lists_every_profile_and_its_models(self):
        write(self.paths["PROFILES"] / "alpha" / "codex-config.toml", CODEX_CONFIG)
        write(self.paths["PROFILES"] / "alpha" / "models.json", MODELS)
        write(self.paths["PROFILES"] / "alpha" / "claude-settings.json",
              {"env": {"ANTHROPIC_BASE_URL": "https://alpha.example"}, "model": "m-one"})
        write(self.paths["PROFILES"] / "beta" / "codex-config.toml", 'model = "beta-model"\nmodel_provider = "p"\n')
        write(self.paths["CURRENT"], "alpha\n")
        self.assertEqual(run(self, "current"), 0)
        self.assertIn("2 profile(s)", self.out)
        self.assertIn("alpha", self.out)
        self.assertIn("active", self.out)
        self.assertIn("beta", self.out)
        self.assertIn("m-one", self.out)          # the models of the active profile are listed
        self.assertIn("m-two", self.out)
        self.assertIn("old.example", self.out)   # the host comes from the profile's Codex provider
        self.assertIn("beta-model", self.out)

    def test_current_plain_prints_only_the_active_name(self):
        write(self.paths["PROFILES"] / "alpha" / "codex-config.toml", CODEX_CONFIG)
        write(self.paths["CURRENT"], "alpha\n")
        self.assertEqual(run(self, "current", "--plain"), 0)
        self.assertEqual(self.out.strip(), "alpha")

    def test_use_plain_skips_the_animation_and_keeps_the_facts(self):
        write(self.paths["PROFILES"] / "p" / "codex-config.toml", CODEX_CONFIG)
        write(self.paths["PROFILES"] / "p" / "models.json", MODELS)
        write(self.paths["CODEX"], CODEX_CONFIG)
        self.assertEqual(run(self, "use", "p", "--plain", "--no-check"), 0)
        self.assertIn("Wrote ~/.codex/models.json", self.out)
        self.assertIn("Active profile: p", self.out)
        self.assertNotIn("█", self.out)


class CatalogTest(unittest.TestCase):
    def test_build_codex_catalog_keeps_hand_written_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write(d / "codex-models.json", {"models": [{"slug": "keep-me", "note": "hand written"}]})
            catalog = {"models": [{"slug": "m-one", "codex": {"slug": "m-one"}}]}
            built = ai_switch.build_codex_catalog(d, catalog)
            slugs = [m["slug"] for m in built["models"]]
            self.assertIn("keep-me", slugs)
            self.assertIn("m-one", slugs)


# ------------------------------------------------------------------ gateway patch
# Codex writes an assistant message item between a turn's tool calls and their results;
# a gateway that translates Responses into Chat completions rejects the next request
# because of it.  The proxy moves that message in front of the calls.
PARATERA_CONFIG = ('model = "m-one"\nmodel_provider = "custom"\n'
                   'model_catalog_json = "~/.codex/models.json"\n\n'
                   '[model_providers.custom]\nname = "custom"\n'
                   'base_url = "https://llmapi.paratera.com"\nwire_api = "responses"\n')


def tool_call(call_id, command="pwd"):
    return {"type": "function_call", "id": call_id, "name": "exec_command",
            "call_id": call_id, "arguments": json.dumps({"cmd": command})}


def tool_result(call_id):
    return {"type": "function_call_output", "id": "fco-" + call_id, "call_id": call_id,
            "output": "/home/x"}


def chat_message(role, text):
    return {"type": "message", "id": "msg-" + role, "role": role,
            "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]}


def codex_turn(text="running both"):
    """Exactly what Codex 0.154 sends back after a turn of two parallel tool calls."""
    return [chat_message("user", "run pwd and date"), tool_call("call_1"), tool_call("call_2"),
            chat_message("assistant", text), tool_result("call_1"), tool_result("call_2")]


class RewriteToolItemsTest(unittest.TestCase):
    def test_codex_message_moves_in_front_of_the_calls(self):
        rewritten, moved, dropped = ai_switch.rewrite_tool_items(codex_turn())
        self.assertEqual((moved, dropped), (1, 0))
        # message(user), message(assistant) + calls + results - the shape a chat API wants
        self.assertEqual([item["type"] for item in rewritten],
                         ["message", "message", "function_call", "function_call",
                          "function_call_output", "function_call_output"])
        self.assertEqual(rewritten[1]["content"][0]["text"], "running both")
        self.assertEqual(rewritten[2]["call_id"], "call_1")
        self.assertEqual(rewritten[-1]["call_id"], "call_2")

    def test_empty_message_is_dropped(self):
        rewritten, moved, dropped = ai_switch.rewrite_tool_items(codex_turn(text=""))
        self.assertEqual((moved, dropped), (0, 1))
        self.assertEqual([item["type"] for item in rewritten],
                         ["message", "function_call", "function_call",
                          "function_call_output", "function_call_output"])

    def test_a_message_before_the_calls_is_left_alone(self):
        items = [chat_message("user", "hi"), chat_message("assistant", "on it"),
                 tool_call("call_1"), tool_result("call_1")]
        self.assertEqual(ai_switch.rewrite_tool_items(items), (items, 0, 0))

    def test_a_message_after_the_results_is_left_alone(self):
        items = [chat_message("user", "hi"), tool_call("call_1"), tool_result("call_1"),
                 chat_message("assistant", "done")]
        self.assertEqual(ai_switch.rewrite_tool_items(items), (items, 0, 0))

    def test_a_run_of_messages_keeps_the_text_and_drops_the_empty_one(self):
        items = [chat_message("user", "hi"), tool_call("call_1"), chat_message("assistant", ""),
                 chat_message("assistant", "running"), tool_result("call_1")]
        rewritten, moved, dropped = ai_switch.rewrite_tool_items(items)
        self.assertEqual((moved, dropped), (1, 1))
        self.assertEqual([item["type"] for item in rewritten],
                         ["message", "message", "function_call", "function_call_output"])

    def test_calls_without_a_message_are_left_alone(self):
        items = [chat_message("user", "hi"), tool_call("call_1"), tool_result("call_1")]
        self.assertEqual(ai_switch.rewrite_tool_items(items), (items, 0, 0))

    def test_an_unfinished_turn_is_left_alone(self):
        items = [chat_message("user", "hi"), tool_call("call_1"), tool_call("call_2"),
                 chat_message("assistant", "running both")]
        self.assertEqual(ai_switch.rewrite_tool_items(items), (items, 0, 0))

    def test_freeform_custom_tool_calls_are_handled(self):
        items = [chat_message("user", "patch it"),
                 {"type": "custom_tool_call", "id": "call_1", "call_id": "call_1",
                  "name": "apply_patch", "input": "*** Begin Patch\n"},
                 chat_message("assistant", "applying"),
                 {"type": "custom_tool_call_output", "id": "ctco-1", "call_id": "call_1",
                  "output": "Done!"}]
        rewritten, moved, dropped = ai_switch.rewrite_tool_items(items)
        self.assertEqual((moved, dropped), (1, 0))
        self.assertEqual([item["type"] for item in rewritten],
                         ["message", "message", "custom_tool_call", "custom_tool_call_output"])

    def test_only_the_tool_turn_is_touched(self):
        items = [chat_message("user", "hi"), chat_message("assistant", "hello")] + codex_turn()[1:]
        rewritten, moved, _ = ai_switch.rewrite_tool_items(items)
        self.assertEqual(moved, 1)
        self.assertEqual(rewritten[:2], items[:2])


class GatewayPatchTest(unittest.TestCase):
    def test_hosts_are_matched_on_the_suffix(self):
        self.assertIsNotNone(ai_switch.gateway_patch_for("https://llmapi.paratera.com"))
        self.assertIsNotNone(ai_switch.gateway_patch_for("https://paratera.com/v1"))
        self.assertIsNotNone(ai_switch.gateway_patch_for("http://paratera.com:8080"))
        self.assertIsNone(ai_switch.gateway_patch_for("https://api.deepseek.com/"))
        self.assertIsNone(ai_switch.gateway_patch_for("https://paratera.com.evil.test"))
        self.assertIsNone(ai_switch.gateway_patch_for("https://notparatera.com"))
        self.assertIsNone(ai_switch.gateway_patch_for(""))

    def test_patch_is_needed_for_the_responses_api_only(self):
        self.assertIsNotNone(ai_switch.patch_needed(PARATERA_CONFIG))
        self.assertIsNone(ai_switch.patch_needed(CODEX_CONFIG))                 # other provider
        for wire in ('wire_api = "chat"', 'wire_api = "other"'):
            changed = PARATERA_CONFIG.replace('wire_api = "responses"', wire)
            self.assertIsNone(ai_switch.patch_needed(changed))

    def test_base_url_is_repointed_in_the_provider_section(self):
        text = ai_switch.set_provider_base_url(PARATERA_CONFIG, "http://127.0.0.1:8791")
        self.assertIn('base_url = "http://127.0.0.1:8791"', text)
        self.assertNotIn("paratera.com", text)
        self.assertIn('[model_providers.custom]', text)
        self.assertIn('wire_api = "responses"', text)
        self.assertEqual(ai_switch.set_provider_base_url("# nothing to do\n", "http://x"),
                         "# nothing to do\n")

    def test_a_profile_without_a_base_url_is_untouched(self):
        self.assertIsNone(ai_switch.patch_needed('model = "m"\n'))


class StubEndpoint:
    """Stands in for the provider: records what the proxy forwarded, answers on demand."""

    def __init__(self, stream=False):
        self.bodies, self.headers, self.stream = [], [], stream
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                endpoint.bodies.append(json.loads(self.rfile.read(length) or b"{}"))
                endpoint.headers.append(dict(self.headers))
                if endpoint.stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for piece in (b'data: {"delta":1}\n\n', b"data: [DONE]\n\n"):
                        self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    return
                body = json.dumps({"answer": "stub"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class PatchProxyTest(unittest.TestCase):
    """The proxy end to end: a real subprocess in front of a stub provider."""

    def setUp(self):
        self.paths = sandbox(self)
        self.stub = StubEndpoint()
        self.addCleanup(self.stub.stop)
        self.addCleanup(ai_switch.stop_patch)
        self.record = ai_switch.start_patch(self.stub.url)
        self.base = f"http://127.0.0.1:{self.record['port']}"

    def post(self, payload, path="/v1/responses"):
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                        headers={"Authorization": "Bearer secret-token",
                                                 "Content-Type": "application/json",
                                                 "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")

    def test_the_body_is_rewritten_on_the_way_through(self):
        status, answer = self.post({"model": "m", "input": codex_turn(), "stream": False})
        self.assertEqual((status, answer), (200, {"answer": "stub"}))
        forwarded = self.stub.bodies[0]["input"]
        self.assertEqual([item["type"] for item in forwarded],
                         ["message", "message", "function_call", "function_call",
                          "function_call_output", "function_call_output"])

    def test_the_key_is_forwarded_and_gzip_is_not_asked_for(self):
        self.post({"model": "m", "input": codex_turn()})
        sent = self.stub.headers[0]
        self.assertEqual(sent.get("Authorization"), "Bearer secret-token")
        self.assertNotEqual(sent.get("Accept-Encoding"), "gzip")   # we relay bytes, not encodings

    def test_other_paths_pass_through_untouched(self):
        self.post({"model": "m", "input": codex_turn()}, path="/v1/responses/other")
        self.assertEqual(self.stub.bodies[0]["input"], codex_turn())

    def test_health_endpoint_identifies_the_proxy(self):
        health = ai_switch.patch_health(self.record["port"])
        self.assertEqual(health["ai_switch_patch"], ai_switch.VERSION)
        self.assertEqual(health["upstream"], self.stub.url)
        self.assertTrue(ai_switch.patch_status()["running"])

    def test_the_log_never_records_the_key(self):
        self.post({"model": "m", "input": codex_turn()})
        log = ai_switch.patch_log_path().read_text()
        self.assertNotIn("secret-token", log)
        self.assertIn("moved=1", log)

    def test_stop_patch_ends_the_process_and_clears_the_record(self):
        self.assertEqual(ai_switch.stop_patch(), [self.record["port"]])
        self.assertFalse(ai_switch.patch_status()["running"])
        self.assertEqual(ai_switch.patch_record(), {})

    def test_an_upstream_error_is_reported_not_hidden(self):
        self.stub.stop()                                     # provider goes away
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post({"model": "m", "input": codex_turn()})
        self.assertEqual(caught.exception.code, 502)

    def test_a_chunked_request_body_is_read_and_rewritten(self):
        body = json.dumps({"model": "m", "input": codex_turn()}).encode()
        with socket.create_connection(("127.0.0.1", self.record["port"]), timeout=15) as link:
            head = (f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    f"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
                    f"Connection: close\r\n\r\n").encode()
            link.sendall(head)
            for start in range(0, len(body), 64):            # deliberately split up
                piece = body[start:start + 64]
                link.sendall(b"%x\r\n" % len(piece) + piece + b"\r\n")
            link.sendall(b"0\r\n\r\n")
            while link.recv(65536):
                pass
        forwarded = self.stub.bodies[0]["input"]
        self.assertEqual([item["type"] for item in forwarded],
                         ["message", "message", "function_call", "function_call",
                          "function_call_output", "function_call_output"])


class PatchProxyStreamTest(unittest.TestCase):
    def setUp(self):
        self.paths = sandbox(self)
        self.stub = StubEndpoint(stream=True)
        self.addCleanup(self.stub.stop)
        self.addCleanup(ai_switch.stop_patch)
        self.record = ai_switch.start_patch(self.stub.url)

    def test_a_streamed_answer_is_relayed_chunk_by_chunk(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.record['port']}/v1/responses",
                                        data=json.dumps({"input": codex_turn(), "stream": True}).encode(),
                                        headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            self.assertEqual(response.headers.get("Transfer-Encoding"), "chunked")
            self.assertEqual(response.read(), b'data: {"delta":1}\n\ndata: [DONE]\n\n')
        self.assertEqual(len(self.stub.bodies), 1)


class PatchLifecycleTest(unittest.TestCase):
    """ai-switch use starts the proxy for a patched gateway and stops it again."""

    def setUp(self):
        self.paths = sandbox(self)
        self.addCleanup(ai_switch.stop_patch)
        write(self.paths["CODEX"], CODEX_CONFIG)
        write(self.paths["CODEX_MODELS"], {"models": [{"slug": "m-one"}]})
        self.para = self.paths["PROFILES"] / "para"
        write(self.para / "codex-config.toml", PARATERA_CONFIG)
        write(self.para / "models.json", MODELS)
        write(self.para / "claude-settings.json",
              {"env": {"ANTHROPIC_BASE_URL": "https://llmapi.paratera.com/anthropic"}})
        self.other = self.paths["PROFILES"] / "other"
        write(self.other / "codex-config.toml", CODEX_CONFIG)
        write(self.other / "models.json", MODELS)

    def test_use_routes_codex_through_the_proxy_and_the_switch_stops_it(self):
        self.assertEqual(run(self, "use", "para", "--no-check"), 0)
        config = self.paths["CODEX"].read_text()
        self.assertIn(f'base_url = "http://127.0.0.1:{ai_switch.patch_record()["port"]}"', config)
        self.assertNotIn("paratera.com", config)
        self.assertIn("patch proxy", self.err)
        self.assertTrue(ai_switch.patch_status()["running"])
        # Claude Code still talks to the provider directly, and the profile is untouched
        self.assertEqual(json.loads(self.paths["CLAUDE"].read_text())["env"]["ANTHROPIC_BASE_URL"],
                         "https://llmapi.paratera.com/anthropic")
        self.assertIn("https://llmapi.paratera.com", (self.para / "codex-config.toml").read_text())

        self.assertEqual(run(self, "use", "other", "--no-check"), 0)
        self.assertIn('base_url = "https://old.example/v1"', self.paths["CODEX"].read_text())
        self.assertEqual(ai_switch.patch_record(), {})
        self.assertFalse(ai_switch.patch_status()["running"])
        self.assertIn("stopped the gateway patch proxy", self.err)

    def test_activating_the_same_profile_again_reuses_the_proxy(self):
        run(self, "use", "para", "--no-check")
        port = ai_switch.patch_record()["port"]
        self.assertEqual(run(self, "use", "para", "--no-check"), 0)
        self.assertEqual(ai_switch.patch_record()["port"], port)

    def test_switching_away_and_back_keeps_the_same_port(self):
        run(self, "use", "para", "--no-check")
        # The port its own previous proxy left in TIME_WAIT must still be usable, or every
        # switch back would move Codex to a different port.
        self.assertEqual(run(self, "use", "other", "--no-check"), 0)
        self.assertEqual(run(self, "use", "para", "--no-check"), 0)
        self.assertEqual(ai_switch.patch_record()["port"], ai_switch.PATCH_PORT)

    def test_no_patch_leaves_codex_on_the_provider_and_stops_the_proxy(self):
        run(self, "use", "para", "--no-check")
        self.assertTrue(ai_switch.patch_status()["running"])
        self.assertEqual(run(self, "use", "para", "--no-check", "--no-patch"), 0)
        self.assertIn('base_url = "https://llmapi.paratera.com"', self.paths["CODEX"].read_text())
        self.assertFalse(ai_switch.patch_status()["running"])
        self.assertEqual(ai_switch.patch_record(), {})

    def test_dry_run_starts_nothing(self):
        self.assertEqual(run(self, "use", "para", "--no-check", "-n"), 0)
        self.assertFalse(ai_switch.patch_status()["running"])
        self.assertIn("dry-run: would route Codex", self.err)
        self.assertEqual(self.paths["CODEX"].read_text(), CODEX_CONFIG)

    def test_dry_run_leaves_a_running_proxy_alone(self):
        run(self, "use", "para", "--no-check")
        port = ai_switch.patch_record()["port"]
        self.assertEqual(run(self, "use", "other", "--no-check", "-n"), 0)
        self.assertIn("dry-run: would stop the gateway patch proxy", self.err)
        self.assertEqual(ai_switch.patch_record()["port"], port)
        self.assertTrue(ai_switch.patch_status()["running"])

    def test_activating_without_the_patch_stops_a_running_proxy_on_dry_run_too(self):
        run(self, "use", "para", "--no-check")
        self.assertEqual(run(self, "use", "para", "--no-check", "--no-patch", "-n"), 0)
        self.assertTrue(ai_switch.patch_status()["running"])
        self.assertIn("dry-run: would stop the gateway patch proxy", self.err)

    def test_doctor_reports_a_proxy_that_is_not_running(self):
        write(self.paths["CODEX"], PARATERA_CONFIG.replace("https://llmapi.paratera.com",
                                                           f"http://127.0.0.1:{ai_switch.PATCH_PORT}"))
        write(self.paths["CURRENT"], "para\n")
        self.assertEqual(run(self, "doctor", "--json"), 1)
        failures = [c for c in json.loads(self.out)["checks"] if c["status"] == "fail"]
        self.assertTrue(any("patch proxy is not running" in c["title"] for c in failures))

    def test_doctor_reports_codex_bypassing_the_proxy(self):
        write(self.paths["CURRENT"], "para\n")
        write(self.paths["CODEX"], PARATERA_CONFIG)
        self.assertEqual(run(self, "doctor", "--json"), 0)
        warnings = [c for c in json.loads(self.out)["checks"] if c["status"] == "warn"]
        self.assertTrue(any("not in the way" in c["title"] for c in warnings))

    def test_current_shows_a_proxy_that_died(self):
        run(self, "use", "para", "--no-check")
        port = ai_switch.patch_record()["port"]
        os.kill(ai_switch.patch_record()["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 5
        while ai_switch.patch_status()["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(run(self, "current"), 0)
        self.assertIn("gateway patch proxy is not running", self.out)
        self.assertNotIn(f"127.0.0.1:{port} → ", self.out)


if __name__ == "__main__":
    unittest.main()
