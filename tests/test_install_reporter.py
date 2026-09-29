"""install-reporter.sh: the one-command reporter install for someone else's machine.

Every test runs it from this checkout against a throwaway $HOME, so nothing
here can touch the real ~/.claude. The collector URL points at a closed port:
the install must not depend on the collector being up, and --check must say
so when it isn't.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "install-reporter.sh")
DEAD_URL = "http://127.0.0.1:9"
TOKEN = "correct-horse-battery-staple"
PAYLOAD = os.path.join(REPO, "tests", "fixtures", "payload.json")

# An existing, user-owned status line: padding and hooks of its own that the
# install must keep.
THEIRS = {
    "model": "opus",
    "statusLine": {"type": "command", "command": "~/.claude/mine.sh", "padding": 2},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
    },
}


class InstallReporterTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        os.makedirs(os.path.join(self.home, ".claude"))
        self.env = {
            "HOME": self.home,
            "PATH": os.environ["PATH"],
            "STATUSGUMBO_STATE_DIR": os.path.join(self.home, "state"),
        }

    def path(self, *parts):
        return os.path.join(self.home, *parts)

    @property
    def settings_path(self):
        return self.path(".claude", "settings.json")

    def write_settings(self, data):
        with open(self.settings_path, "w") as fh:
            json.dump(data, fh)

    def settings(self):
        with open(self.settings_path) as fh:
            return json.load(fh)

    def run_script(self, *args, cwd=None):
        return subprocess.run(
            ["sh", SCRIPT, *args], env=self.env, cwd=cwd or self.home,
            capture_output=True, text=True, timeout=60,
        )

    def install(self, *extra):
        result = self.run_script("--url", DEAD_URL, *extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def ours(self, event):
        return [
            entry for entry in self.settings()["hooks"].get(event, [])
            if any("statusgumbo-report" in h.get("command", "") for h in entry["hooks"])
        ]


class TestFreshMachine(InstallReporterTestCase):
    """No settings file, no status line: the reporter's wrapper becomes the
    status line and draws nothing."""

    def test_status_line_is_the_wrapper_with_the_heartbeat(self):
        self.install()
        line = self.settings()["statusLine"]
        self.assertIn("statusgumbo-statusline.sh", line["command"])
        self.assertEqual(line["refreshInterval"], 10)

    def test_both_waiting_hooks_are_added(self):
        self.install()
        [pre] = self.ours("PreToolUse")
        self.assertEqual(pre["matcher"], "AskUserQuestion|ExitPlanMode")
        self.assertEqual(len(self.ours("PermissionRequest")), 1)
        for entry in (pre, self.ours("PermissionRequest")[0]):
            self.assertIn("--wait", entry["hooks"][0]["command"])

    def test_files_are_installed_executable(self):
        self.install()
        for name in ("statusgumbo-report.sh", "statusgumbo-statusline.sh"):
            mode = os.stat(self.path(".claude", name)).st_mode
            self.assertTrue(mode & stat.S_IXUSR, name)

    def test_url_token_and_place_are_saved_as_files(self):
        self.install("--token", TOKEN, "--place", "laptop")
        cfg = self.path(".config", "statusgumbo")
        with open(os.path.join(cfg, "url")) as fh:
            self.assertEqual(fh.read().strip(), DEAD_URL)
        with open(os.path.join(cfg, "token")) as fh:
            self.assertEqual(fh.read().strip(), TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(cfg, "token")).st_mode), 0o600)
        with open(os.path.join(cfg, "place")) as fh:
            self.assertEqual(fh.read().strip(), "laptop")

    def test_a_malformed_token_is_refused_before_anything_is_written(self):
        result = self.run_script("--url", DEAD_URL, "--token", "short")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(self.settings_path))

    def test_a_url_that_is_not_http_is_refused(self):
        result = self.run_script("--url", "file:///etc/passwd")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(self.settings_path))

    def test_the_wrapper_alone_draws_nothing(self):
        self.install()
        with open(PAYLOAD) as fh:
            result = subprocess.run(
                [self.path(".claude", "statusgumbo-statusline.sh")],
                stdin=fh, env=self.env, capture_output=True, text=True, timeout=10,
            )
        self.assertEqual((result.returncode, result.stdout), (0, ""))


class TestExistingStatusLine(InstallReporterTestCase):
    """The machine's own status line is wrapped, never edited or dropped."""

    def setUp(self):
        super().setUp()
        self.write_settings(THEIRS)
        with open(self.path(".claude", "mine.sh"), "w") as fh:
            fh.write('#!/bin/sh\ncat >/dev/null\nprintf "MY LINE"\nexit 3\n')
        os.chmod(self.path(".claude", "mine.sh"), 0o755)

    def test_the_original_command_is_saved_and_its_other_keys_kept(self):
        self.install()
        with open(self.path(".config", "statusgumbo", "statusline-command")) as fh:
            self.assertEqual(fh.read().strip(), "~/.claude/mine.sh")
        line = self.settings()["statusLine"]
        self.assertIn("statusgumbo-statusline.sh", line["command"])
        self.assertEqual(line["padding"], 2)
        self.assertEqual(self.settings()["model"], "opus")

    def test_the_users_own_hooks_survive(self):
        self.install()
        hooks = self.settings()["hooks"]
        self.assertEqual(hooks["Stop"], THEIRS["hooks"]["Stop"])
        self.assertIn(THEIRS["hooks"]["PreToolUse"][0], hooks["PreToolUse"])

    def test_the_wrapped_line_renders_exactly_as_before(self):
        # Same output and the same exit code: the wrapper is invisible.
        self.install()
        with open(PAYLOAD) as fh:
            result = subprocess.run(
                [self.path(".claude", "statusgumbo-statusline.sh")],
                stdin=fh, env=self.env, capture_output=True, text=True, timeout=10,
            )
        self.assertEqual((result.returncode, result.stdout), (3, "MY LINE"))

    def test_running_it_twice_changes_nothing(self):
        # The second run must not save the wrapper as "the original".
        self.install()
        first = self.settings()
        self.install()
        self.assertEqual(self.settings(), first)
        with open(self.path(".config", "statusgumbo", "statusline-command")) as fh:
            self.assertEqual(fh.read().strip(), "~/.claude/mine.sh")

    def test_an_existing_refresh_interval_is_kept(self):
        data = json.loads(json.dumps(THEIRS))
        data["statusLine"]["refreshInterval"] = 5
        self.write_settings(data)
        self.install()
        self.assertEqual(self.settings()["statusLine"]["refreshInterval"], 5)

    def test_a_backup_of_the_original_file_is_left(self):
        self.install()
        backups = [n for n in os.listdir(self.path(".claude"))
                   if n.startswith("settings.json.statusgumbo-")]
        self.assertEqual(len(backups), 1)
        with open(self.path(".claude", backups[0])) as fh:
            self.assertEqual(json.load(fh), THEIRS)

    def test_uninstall_puts_everything_back(self):
        self.install()
        result = self.run_script("--uninstall")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.settings(), THEIRS)
        self.assertFalse(os.path.exists(self.path(".claude", "statusgumbo-statusline.sh")))


class TestAStatusLineThatAlreadyReports(InstallReporterTestCase):
    """The author's setup: the snippet is already appended to the machine's own
    script. Wrapping that would post every tick twice."""

    def test_it_is_not_wrapped(self):
        script = self.path(".claude", "statusline.sh")
        with open(script, "w") as fh:
            fh.write('#!/bin/sh\ninput=$(cat)\nprintf x\n'
                     'printf "%s" "$input" | "$HOME/.claude/statusgumbo-report.sh"\n')
        self.write_settings({"statusLine": {"type": "command", "command": "~/.claude/statusline.sh"}})
        result = self.install()
        line = self.settings()["statusLine"]
        self.assertEqual(line["command"], "~/.claude/statusline.sh")
        self.assertEqual(line["refreshInterval"], 10)
        self.assertEqual(len(self.ours("PermissionRequest")), 1)
        self.assertIn("already", result.stdout)


class TestUnsafeSettings(InstallReporterTestCase):
    def test_invalid_json_is_left_untouched(self):
        with open(self.settings_path, "w") as fh:
            fh.write("{ not json")
        result = self.run_script("--url", DEAD_URL)
        self.assertNotEqual(result.returncode, 0)
        with open(self.settings_path) as fh:
            self.assertEqual(fh.read(), "{ not json")

    def test_a_symlinked_settings_file_stays_a_symlink(self):
        # Dotfiles repos link ~/.claude/settings.json; replacing the link with
        # a file would silently detach the machine from its dotfiles.
        target = self.path("dotfiles-settings.json")
        with open(target, "w") as fh:
            json.dump({"model": "opus"}, fh)
        os.symlink(target, self.settings_path)
        self.install()
        self.assertTrue(os.path.islink(self.settings_path))
        with open(target) as fh:
            self.assertIn("statusLine", json.load(fh))


class TestCheck(InstallReporterTestCase):
    def test_a_dropped_refresh_interval_is_reported(self):
        # What Claude Code and some provisioning tools do to settings.json.
        self.install()
        data = self.settings()
        del data["statusLine"]["refreshInterval"]
        self.write_settings(data)
        result = self.run_script("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refreshInterval", result.stdout)

    def test_an_unreachable_collector_is_reported(self):
        self.install()
        result = self.run_script("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(DEAD_URL, result.stdout)

    def test_a_project_status_line_that_wins_is_reported(self):
        self.install()
        project = self.path("proj")
        os.makedirs(os.path.join(project, ".claude"))
        with open(os.path.join(project, ".claude", "settings.json"), "w") as fh:
            json.dump({"statusLine": {"type": "command", "command": "echo hi"}}, fh)
        result = self.run_script("--check", cwd=project)
        self.assertIn(".claude/settings.json", result.stdout)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
