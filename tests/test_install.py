"""install.sh, run for real against a throwaway HOME and stubbed systemd.

The installer touches $HOME/.claude, $HOME/.config/systemd/user and the
`systemctl`/`tailscale` binaries, all of which are reachable through HOME and
PATH — so it can be exercised end to end without going near the real user
session. That matters here: the finding under test is about `set -eu`
aborting mid-run, which no amount of reading the source proves.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "install.sh")

TAILSCALE_STUB = "#!/bin/sh\necho 100.64.0.1\n"

# Installed, but with no address to give: tailscaled down, or logged out.
# `tailscale ip -4` exits non-zero and prints nothing.
TAILSCALE_NO_ADDRESS = "#!/bin/sh\nexit 1\n"

SYSTEMCTL_OK = "#!/bin/sh\nexit 0\n"

# Fails only the tunnel unit, exactly the case in the finding: the collector
# unit is already enabled by then, so `set -e` would abort with the job half
# done and swallow the closing "open http://..." line, which is the whole
# reason the operator ran the script.
SYSTEMCTL_TUNNEL_FAILS = """#!/bin/sh
for arg in "$@"; do
    if [ "$arg" = "statusgumbo-tunnel.service" ]; then
        echo "Failed to enable unit: stub failure" >&2
        exit 1
    fi
done
exit 0
"""


class InstallScriptTestCase(unittest.TestCase):
    def run_installer(
        self,
        systemctl_stub,
        with_tunnel_env=True,
        existing=None,
        tailscale_stub=TAILSCALE_STUB,
        install=INSTALL,
    ):
        """Run install.sh against a throwaway $HOME.

        `existing` seeds files into that HOME before the run, as {relpath:
        contents} — the point being to model a machine that already has a
        status line of its own, which is the case the installer must not
        trample. self.home is left pointing at the sandbox so tests can
        inspect what survived.
        """
        home = tempfile.mkdtemp(prefix="statusgumbo-home-")
        self.addCleanup(shutil.rmtree, home, True)
        self.home = home
        for relpath, contents in (existing or {}).items():
            path = os.path.join(home, relpath)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(contents)
        stubs = os.path.join(home, "stubs")
        os.makedirs(stubs)
        for name, body in (
            ("systemctl", systemctl_stub),
            ("tailscale", tailscale_stub),
        ):
            path = os.path.join(stubs, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)

        if with_tunnel_env:
            conf = os.path.join(home, ".config", "statusgumbo")
            os.makedirs(conf)
            with open(os.path.join(conf, "tunnel.env"), "w", encoding="utf-8") as fh:
                fh.write("CODER_HOST=example\n")

        env = dict(os.environ)
        env["HOME"] = home
        env["PATH"] = stubs + os.pathsep + env["PATH"]
        return subprocess.run(
            ["sh", install],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )


class TestInstallerSyntax(InstallScriptTestCase):
    def test_the_script_parses(self):
        result = subprocess.run(
            ["sh", "-n", INSTALL], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class TestInstallerHappyPath(InstallScriptTestCase):
    def test_it_finishes_and_prints_the_phone_url(self):
        result = self.run_installer(SYSTEMCTL_OK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("started statusgumbo-tunnel.service", result.stdout)
        self.assertIn("http://100.64.0.1:4747", result.stdout)


class TestInstalledTunnelUnitPointsAtThisCheckout(InstallScriptTestCase):
    """The tunnel unit gained a path and therefore needs substituting.

    It used to be copied verbatim, because `ExecStart=/usr/bin/ssh` is the
    same everywhere. Now that ExecStart is this repo's tunnel.sh, a verbatim
    copy installs a literal `@REPO@` and the unit fails at start with a
    203/EXEC that says nothing about why — the collector unit has been
    substituted all along, and the two must not diverge.
    """

    def installed_unit(self):
        path = os.path.join(
            self.home, ".config", "systemd", "user", "statusgumbo-tunnel.service"
        )
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_no_placeholder_survives_into_the_installed_unit(self):
        self.run_installer(SYSTEMCTL_OK)
        self.assertNotIn("@REPO@", self.installed_unit())

    def test_it_runs_the_wrapper_from_this_checkout(self):
        self.run_installer(SYSTEMCTL_OK)
        self.assertIn(os.path.join(REPO, "contrib", "coder", "tunnel.sh"), self.installed_unit())


class TestTunnelFailureIsNotFatal(InstallScriptTestCase):
    """A failing tunnel unit used to abort the whole installer under `set -eu`
    after the collector unit was already enabled, and swallow the closing
    "open http://<tailnet-ip>:4747" line — the point of running it.
    """

    def test_the_installer_still_completes(self):
        result = self.run_installer(SYSTEMCTL_TUNNEL_FAILS)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_phone_url_is_still_printed(self):
        result = self.run_installer(SYSTEMCTL_TUNNEL_FAILS)
        self.assertIn("http://100.64.0.1:4747", result.stdout)

    def test_the_failure_is_reported_with_a_way_to_diagnose_it(self):
        result = self.run_installer(SYSTEMCTL_TUNNEL_FAILS)
        combined = result.stdout + result.stderr
        self.assertIn("journalctl --user -u statusgumbo-tunnel", combined)


class TestInstallerNeverTramplesAStatusLine(InstallScriptTestCase):
    """The installer must not replace a status line the machine already has.

    It used to, backing the old file up first, which reads as safe. On the
    first real deployment the file it displaced was newer than this repo's
    copy: a status line maintained in a dotfiles repo that this repo had
    forked a day earlier and then fallen behind. The install silently
    downgraded the thing it exists to report on, and the backup went unread.

    "Newer" is not something the installer can detect, so it does not try.
    It never replaces, and prints the hook instead.
    """

    OWN_LINE = '#!/bin/sh\ninput=$(cat)\nprintf "MY OWN STATUS LINE"\n'

    def test_an_existing_status_line_survives_byte_for_byte(self):
        self.run_installer(
            SYSTEMCTL_OK, existing={".claude/statusline.sh": self.OWN_LINE}
        )
        with open(
            os.path.join(self.home, ".claude", "statusline.sh"), encoding="utf-8"
        ) as fh:
            self.assertEqual(fh.read(), self.OWN_LINE)

    def test_it_prints_the_hook_to_add_instead(self):
        result = self.run_installer(
            SYSTEMCTL_OK, existing={".claude/statusline.sh": self.OWN_LINE}
        )
        self.assertIn("statusgumbo-report.sh", result.stdout)
        self.assertIn("kept your existing", result.stdout)

    def test_it_does_not_nag_when_the_hook_is_already_there(self):
        hooked = self.OWN_LINE + (
            'if [ -x "$HOME/.claude/statusgumbo-report.sh" ]; then\n'
            '    printf "%s" "$input" | "$HOME/.claude/statusgumbo-report.sh"\n'
            "fi\n"
        )
        result = self.run_installer(
            SYSTEMCTL_OK, existing={".claude/statusline.sh": hooked}
        )
        self.assertIn("already calls the reporter", result.stdout)
        self.assertNotIn("kept your existing", result.stdout)

    def test_it_installs_no_status_line_when_the_machine_has_none(self):
        """It used to link this repo's own copy here. That copy was a fork, and
        it drifted three weeks behind the original without its golden test
        noticing — the render differed by one colour code. The fork is gone;
        a machine with no status line is told where to get one."""
        result = self.run_installer(SYSTEMCTL_OK)
        target = os.path.join(self.home, ".claude", "statusline.sh")
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.islink(target))
        self.assertIn("no status line found", result.stdout)

    def test_the_reporter_is_linked_when_absent(self):
        self.run_installer(SYSTEMCTL_OK)
        reporter = os.path.join(self.home, ".claude", "statusgumbo-report.sh")
        self.assertTrue(os.path.islink(reporter))
        self.assertEqual(os.path.realpath(reporter), os.path.join(REPO, "report.sh"))

    def test_an_existing_reporter_is_left_alone(self):
        """On a machine whose HOME *is* a dotfiles working tree, this path is a
        tracked file. Replacing it with a symlink dirties that repo with a type
        change, which is exactly the mess this whole exercise is unpicking.
        """
        vendored = "#!/bin/sh\n# vendored copy\n"
        self.run_installer(
            SYSTEMCTL_OK, existing={".claude/statusgumbo-report.sh": vendored}
        )
        reporter = os.path.join(self.home, ".claude", "statusgumbo-report.sh")
        self.assertFalse(os.path.islink(reporter))
        with open(reporter, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), vendored)


class TestTunnelUnitOwnsItsConnection(unittest.TestCase):
    """The tunnel must not ride on the user's SSH connection multiplexer.

    Found on the first real deployment. A `Host *` block with `ControlMaster
    auto` is a common setup, and under it `ssh -N` never opens a connection of
    its own: it hands the forward to the existing mux master and exits 0
    immediately, its work done. systemd sees the main process exit, Restart
    fires, and the unit flaps forever — silently, because nothing failed and
    nothing is logged.

    The forward does come up, which is what makes this so easy to miss, but it
    lives in the master and dies with it at ControlPersist. Pinning both flags
    forces a dedicated connection whose lifetime is the unit's.

    The flags moved from the unit's ExecStart into tunnel.sh when the retry
    policy did; the invariant is unchanged and this is still where it is
    pinned.
    """

    def setUp(self):
        with open(os.path.join(REPO, "contrib", "coder", "tunnel.sh"), encoding="utf-8") as fh:
            self.wrapper = fh.read()

    def test_it_does_not_join_an_existing_mux_master(self):
        self.assertIn("ControlPath=none", self.wrapper)

    def test_it_does_not_become_a_mux_master_either(self):
        self.assertIn("ControlMaster=no", self.wrapper)


class TestTheHookDoesNotDependOnTheParentShell(unittest.TestCase):
    """Reporting must not require the shell that launched Claude Code to have
    exported STATUSGUMBO_URL.

    Found on 2026-07-30 with the page showing "no sessions reporting" while two
    sessions were live. The deployment exports the URL from ~/.bashrc, the
    operator's interactive shell is fish, and fish does not read .bashrc — so
    report.sh hit its first guard and exited 0. Silently, which is the exact
    failure this project exists to surface: a session that renders perfectly
    and never appears.

    Nothing about that is specific to fish. Any launch path that is not an
    interactive bash — a desktop launcher, a systemd unit, a cron job, zsh —
    reports nothing and says nothing about it.

    The default belongs in the hook rather than in report.sh, which keeps the
    reporter a complete no-op on a machine that never installed it. The hook is
    the opt-in: it only exists in a status line someone deliberately edited.
    """

    HOOK_DEFAULT = 'STATUSGUMBO_URL="${STATUSGUMBO_URL:-http://127.0.0.1:4747}"'

    def test_the_printed_hook_supplies_the_url_itself(self):
        result = subprocess.run(
            ["grep", "-c", "STATUSGUMBO_URL", os.path.join(REPO, "install.sh")],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.stdout.strip(), "0", "install.sh never mentions the URL")

    def test_the_default_is_the_local_collector_and_does_not_override(self):
        # `${VAR:-default}`, not a bare assignment: a machine that does export
        # it — the VM, via .bashrc — must keep its own value.
        with open(os.path.join(REPO, "install.sh"), encoding="utf-8") as fh:
            self.assertIn(self.HOOK_DEFAULT, fh.read())

    def test_the_readme_documents_the_same_hook(self):
        # The README snippet is what people copy; a hook that differs from the
        # installer's is how the two drift apart.
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
            self.assertIn(self.HOOK_DEFAULT, fh.read())


class TestTheCollectorWaitsForTheTailnetAddress(unittest.TestCase):
    """The collector must not be started before Tailscale has an address.

    `collector.server` resolves the bind list once, at startup, and a machine
    with no Tailscale address is a supported case: it binds loopback, warns,
    and keeps running. That fallback is right for a Tailscale outage and wrong
    for a boot race, where the daemon is merely a few seconds behind — the
    collector then serves 127.0.0.1 only, for as long as it runs, and looks
    perfectly healthy while the phone cannot reach it. Restart=always does not
    help; nothing has failed.

    Both this unit and tailscaled start around the same point in boot, and
    `After=network.target` orders against neither. A user unit cannot order
    against a system unit at all, so the wait has to be an ExecStartPre.

    Untested in the wild as of 2026-08-06: the unit was written the day after
    the last reboot, so it has never been through one.
    """

    def setUp(self):
        path = os.path.join(REPO, "systemd", "statusgumbo.service")
        with open(path, encoding="utf-8") as fh:
            self.unit = fh.read()
        self.pre = [
            line for line in self.unit.splitlines() if line.startswith("ExecStartPre=")
        ]

    def test_it_waits_for_the_address_before_starting(self):
        self.assertEqual(len(self.pre), 1, self.unit)
        self.assertIn("tailscale ip -4", self.pre[0])

    def test_the_wait_is_bounded(self):
        # An unreachable daemon must delay the collector, not strand it.
        self.assertIn("timeout ", self.pre[0])

    def test_a_tailscale_that_never_arrives_still_starts_the_collector(self):
        # A *failing* ExecStartPre aborts the whole start job, which would turn
        # every Tailscale outage into no collector at all — strictly worse than
        # the loopback-only fallback it exists to avoid. The trailing `exit 0`
        # is what keeps the timeout a delay rather than a refusal.
        self.assertTrue(
            self.pre[0].rstrip().rstrip("'\"").endswith("exit 0"), self.pre[0]
        )

    def test_the_wait_uses_no_shell_variables(self):
        # systemd expands `$foo` and `${foo}` in ExecStart* itself, so a plain
        # counter loop would have its variable substituted away before /bin/sh
        # ever saw it. `$$` escapes, and is easy to lose in a later edit; the
        # loop is written to need no variable at all.
        self.assertNotIn("$", self.pre[0])


class TestTheCollectorUnitTakesMachineLocalArguments(unittest.TestCase):
    """Options such as --cloud belong to one machine, not to the repo. The
    unit reads them from an optional env file, so the committed unit is the
    same for everyone and a machine without the file runs the defaults."""

    def setUp(self):
        path = os.path.join(REPO, "systemd", "statusgumbo.service")
        with open(path, encoding="utf-8") as fh:
            self.unit = fh.read()

    def execstart(self):
        [line] = [l for l in self.unit.splitlines() if l.startswith("ExecStart=")]
        return line

    def test_the_env_file_is_optional(self):
        self.assertIn(
            "EnvironmentFile=-%h/.config/statusgumbo/collector.env", self.unit
        )

    def test_extra_arguments_come_from_it_as_separate_words(self):
        # Unquoted $VAR as its own word: systemd splits it into zero or more
        # arguments, so an unset variable adds nothing.
        self.assertTrue(self.execstart().endswith(" $STATUSGUMBO_COLLECTOR_ARGS"))

    def test_cloud_is_not_hard_coded(self):
        self.assertNotIn("--cloud", self.execstart())


class TestTheCollectorCannotGainPrivileges(unittest.TestCase):
    def test_no_new_privileges_is_set(self):
        path = os.path.join(REPO, "systemd", "statusgumbo.service")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertIn("NoNewPrivileges=yes", lines)


class TestTheCheckoutPathReachesTheUnitsVerbatim(InstallScriptTestCase):
    """The checkout path is spliced into two units by sed. `&` and `|` mean
    something in a sed replacement, and systemd reads `%`, whitespace, quotes
    and backslashes in its own ways, so the installer is run from a copy of
    the repo at a path holding each kind of character."""

    def run_from(self, dirname):
        parent = tempfile.mkdtemp(prefix="statusgumbo-path-")
        self.addCleanup(shutil.rmtree, parent, True)
        checkout = os.path.join(parent, dirname)
        os.makedirs(checkout)
        shutil.copy2(INSTALL, checkout)
        shutil.copy2(os.path.join(REPO, "report.sh"), checkout)
        shutil.copytree(os.path.join(REPO, "systemd"), os.path.join(checkout, "systemd"))
        shutil.copytree(os.path.join(REPO, "contrib"), os.path.join(checkout, "contrib"))
        install = os.path.join(checkout, "install.sh")
        return checkout, self.run_installer(SYSTEMCTL_OK, install=install)

    def unit(self, name):
        path = os.path.join(self.home, ".config", "systemd", "user", name)
        with open(path, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_sed_metacharacters_are_written_literally(self):
        checkout, result = self.run_from("a&b|c")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WorkingDirectory=" + checkout, self.unit("statusgumbo.service"))
        self.assertIn(
            "ExecStart=" + checkout + "/contrib/coder/tunnel.sh",
            self.unit("statusgumbo-tunnel.service"),
        )

    def test_a_path_systemd_would_misread_is_refused_before_anything_is_installed(self):
        for dirname in ("a b", "a%h", 'a"b', "a'b", "a\\b"):
            with self.subTest(dirname=dirname):
                _, result = self.run_from(dirname)
                self.assertEqual(result.returncode, 1)
                self.assertIn("cannot install from", result.stderr)
                self.assertFalse(
                    os.path.exists(os.path.join(self.home, ".config", "systemd")),
                    "a unit was written before the refusal",
                )
                self.assertFalse(
                    os.path.lexists(
                        os.path.join(self.home, ".claude", "statusgumbo-report.sh")
                    ),
                    "the reporter was linked before the refusal",
                )


class TestTunnelNeverStartsTheWorkspace(unittest.TestCase):
    """A monitoring tunnel must never provision infrastructure.

    Found in production on 2026-07-30. `coder ssh` autostarts a stopped
    workspace by default, and the unit's ExecStart reaches it through the
    ProxyCommand in ~/.ssh/config. So when the workspace went to sleep,
    Restart=always retried every 11 seconds, hammered the Coder API through
    13 "last build job is stopping" errors, and the moment the stop
    completed it triggered a full terraform apply and brought the instance
    back up — 68 seconds of provisioning, plus startup scripts, on a box the
    operator had deliberately let stop. Restart counter reached 21.

    The observability tool woke the thing it was observing. Whether the
    workspace runs is the operator's decision; the tunnel's only business is
    connecting to it if it is already up.

    CODER_SSH_DISABLE_AUTOSTART rather than the --disable-autostart flag
    because the unit executes /usr/bin/ssh, not coder: the coder client is
    the ProxyCommand, so the environment is the only channel that reaches
    it. Editing the ssh config instead would not survive `coder config-ssh`,
    which regenerates that block and says so in a DO NOT EDIT header.
    """

    def setUp(self):
        path = os.path.join(REPO, "contrib", "coder", "statusgumbo-tunnel.service")
        with open(path, encoding="utf-8") as fh:
            self.unit = fh.read()

    def test_autostart_is_disabled_for_the_proxy_command(self):
        self.assertIn("CODER_SSH_DISABLE_AUTOSTART=true", self.unit)

    def test_it_is_set_as_environment_not_as_a_flag(self):
        # An `Environment=` line is inherited by the ProxyCommand child; a
        # flag on the ssh command line would be passed to ssh, which does
        # not understand it, and the tunnel would simply fail to start.
        self.assertIn("Environment=CODER_SSH_DISABLE_AUTOSTART=true", self.unit)
        self.assertNotIn("--disable-autostart", self.unit)

    def test_the_retry_policy_is_not_expressed_in_systemd(self):
        # It was — `RestartSteps` / `RestartMaxDelaySec` growing 10s to 10
        # minutes so a workspace stopped all night was not an API call every
        # ten seconds. The requirement stands; systemd cannot meet it.
        #
        # Those settings derive the delay from `NRestarts`, and systemd 255
        # never decays that counter: `systemctl restart` leaves it untouched,
        # only an explicit stop *then* start clears it, and a long healthy run
        # does not clear it at all. Measured on 2026-07-31, when the unit ran
        # eleven hours and still carried NRestarts=16 from the night before,
        # so it sat permanently at the ceiling. A reverse tunnel cannot
        # survive a laptop suspend and there are several a day; each resume
        # then cost up to ten blind minutes for a link that would have come
        # back on the first try.
        #
        # Backoff that resets on success needs state systemd will not keep, so
        # it lives in tunnel.sh, and tests/test_tunnel.sh pins the behaviour.
        self.assertNotIn("RestartSteps=", self.unit)
        self.assertNotIn("RestartMaxDelaySec=", self.unit)

    def test_restart_remains_as_a_backstop_for_the_wrapper_dying(self):
        # The wrapper loops across dropped connections itself, so this fires
        # only if the wrapper process itself is lost — which must not leave
        # the tunnel permanently gone.
        self.assertIn("Restart=always", self.unit)


if __name__ == "__main__":
    unittest.main()


class TestAMissingTailnetAddressIsNotAnInstallFailure(InstallScriptTestCase):
    """install.sh exited 1 on a machine where the install had SUCCEEDED.

    The closing line was `[ -n "$ip" ] && printf ...`. As the last command of
    the script under `set -e`, a false test makes the whole script exit 1 —
    and the test is false exactly when tailscale is installed but has no
    address yet: tailscaled still starting, stopped, or logged out. Nothing
    had gone wrong; the reporter was linked and both units were enabled.

    Measured on 2026-09-01 in sh, dash and bash — all three exit 1, so it is
    not one shell's quirk. This is the same `[ … ] && …` trap README.md
    documents for the status line and tunnel.sh documents for the supervisor
    loop, sitting in this project's own installer. It is invisible by eye
    because everything printed above it is already correct; only the exit
    status differs, and anything running the installer from a provisioning
    script sees a failed install.
    """

    def test_the_installer_still_succeeds(self):
        result = self.run_installer(
            SYSTEMCTL_OK, tailscale_stub=TAILSCALE_NO_ADDRESS
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_it_says_where_the_page_will_be_instead(self):
        result = self.run_installer(
            SYSTEMCTL_OK, tailscale_stub=TAILSCALE_NO_ADDRESS
        )
        self.assertIn("<tailnet-ip>:4747", result.stdout)

    def test_the_address_is_still_printed_when_there_is_one(self):
        # The fix must not cost the happy path its actual URL.
        result = self.run_installer(SYSTEMCTL_OK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://100.64.0.1:4747", result.stdout)


class TestTheInstallerDoesNotPrintAdviceTheDocsCallWrong(InstallScriptTestCase):
    """The installer used to instruct the operator to reproduce two outages.

    Both are recorded in README.md as mistakes, and the closing
    block of install.sh went on printing them anyway — so anyone who followed
    the script's own output walked into both:

    1. Exporting STATUSGUMBO_URL from a shell profile. It reaches only
       sessions launched from an interactive bash; the shell here is fish, so
       every session rendered a perfect status line and published nothing.
       The hook supplies the default itself, which is why nothing needs
       exporting at all.
    2. Setting STATUSGUMBO_HOST=coder-vm on the VM. `hostname -s` on a Coder
       workspace is the workspace's own name, and setting the override
       somewhere only some sessions inherit split one machine across two host
       badges on the page.

    Asserted against what the installer actually PRINTS, not against the file,
    so the comments explaining why the advice is gone can still name it.
    """

    def printed(self):
        return self.run_installer(SYSTEMCTL_OK).stdout

    def test_it_does_not_tell_you_to_export_the_url_from_a_profile(self):
        out = self.printed()
        self.assertNotIn("export STATUSGUMBO_URL=", out)
        self.assertNotIn("set -Ux STATUSGUMBO_URL", out)

    def test_it_does_not_resurrect_the_coder_vm_host_override(self):
        self.assertNotIn("STATUSGUMBO_HOST=coder-vm", self.printed())

    def test_it_still_explains_the_heartbeat(self):
        # The one manual step that genuinely remains must survive the cut.
        self.assertIn("refreshInterval", self.printed())

    def test_it_warns_that_claude_code_can_strip_the_heartbeat(self):
        # Proven on a fresh workspace 2026-09-01: Claude Code rewrites the
        # statusLine object through the settings symlink and drops the key,
        # so "I set it once" is not the same as "it is set".
        self.assertIn("CHECK IT AGAIN", self.printed())


class TestTheWaitingHooks(InstallScriptTestCase):
    """A session waiting on a dialog must stay on the page.

    Claude Code does not run the status line while a question, plan approval
    or permission prompt is open, so no ticks arrive and the card leaves at
    ACTIVE_SECS — observed 2026-09-16. `report.sh --wait` covers the gap, but
    only if hooks call it, and those live in settings.json, which the
    installer deliberately never edits. So the snippet is printed, and the
    README carries the same one: what people paste has to be what works.
    """

    HOOK_DEFAULT = 'STATUSGUMBO_URL="${STATUSGUMBO_URL:-http://127.0.0.1:4747}"'

    def readme_hooks(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        for block in re.findall(r"```json\n(.*?)```", text, re.S):
            if "PermissionRequest" in block:
                return json.loads(block)["hooks"]
        self.fail("README.md has no ```json block with the waiting hooks")

    def commands(self, hooks):
        return [
            h["command"]
            for event in ("PreToolUse", "PermissionRequest")
            for group in hooks[event]
            for h in group["hooks"]
        ]

    def test_the_readme_hooks_cover_every_dialog_that_stops_the_status_line(self):
        hooks = self.readme_hooks()
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "AskUserQuestion|ExitPlanMode")
        self.assertIn("PermissionRequest", hooks)

    def test_each_hook_calls_wait_with_the_url_supplied(self):
        for command in self.commands(self.readme_hooks()):
            self.assertIn('statusgumbo-report.sh" --wait', command)
            self.assertIn(self.HOOK_DEFAULT, command)

    def test_each_hook_is_a_no_op_without_the_reporter(self):
        # `if`, never `[ -x … ] && …`: a short-circuited && exits 1, which
        # Claude Code reports as a hook error on every machine without it.
        for command in self.commands(self.readme_hooks()):
            self.assertTrue(command.startswith('if [ -x "$HOME/.claude/statusgumbo-report.sh" ]'), command)
            result = subprocess.run(
                ["sh", "-c", command],
                env={"HOME": tempfile.mkdtemp(prefix="statusgumbo-nohook-"), "PATH": os.environ["PATH"]},
                input="{}", capture_output=True, text=True, timeout=10,
            )
            self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)

    def test_the_installer_prints_the_same_hooks(self):
        result = self.run_installer(SYSTEMCTL_OK)
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in self.commands(self.readme_hooks()):
            self.assertIn(json.dumps(command), result.stdout)
