import contextlib, io, json, os, sqlite3, sys, tempfile, unittest
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


if __name__ == "__main__":
    unittest.main()
