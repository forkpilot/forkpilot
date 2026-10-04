"""forkpilot doctor: the decisions are pure functions, the repo checks run against a tiny throwaway repo."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from forkpilot import doctor
from forkpilot.doctor import FAIL, OK, WARN, Check

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def fake_ardupilot() -> tuple[Path, str, str]:
    """A repo shaped like ArduPilot as far as doctor looks: waf, ArduCopter/, copter.parm, two commits."""
    d = Path(tempfile.mkdtemp())
    git(d, "init", "-q")
    (d / "waf").write_text("#!/bin/sh\n")
    (d / "waf").chmod(0o755)
    (d / "ArduCopter").mkdir()
    (d / "ArduCopter" / "Copter.cpp").write_text("// v1\n")
    parm = d / "Tools" / "autotest" / "default_params"
    parm.mkdir(parents=True)
    (parm / "copter.parm").write_text("")
    (d / "modules" / "waf" / "waflib").mkdir(parents=True)
    (d / "modules" / "waf" / "waflib" / "Context.py").write_text("")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "one")
    first = git(d, "rev-parse", "HEAD")
    (d / "ArduCopter" / "Copter.cpp").write_text("// v2\n")
    git(d, "commit", "-qam", "two")
    return d, first, git(d, "rev-parse", "HEAD")


def by_name(checks, prefix):
    return next(c for c in checks if c.name.startswith(prefix))


class PureChecks(unittest.TestCase):
    def test_version_tuple(self):
        self.assertEqual(doctor.version_tuple("3.3.4"), (3, 3, 4))
        self.assertEqual(doctor.version_tuple("4.0.1.post2"), (4, 0, 1))
        self.assertEqual(doctor.version_tuple("3.12.0rc1"), (3, 12, 0))

    def test_python(self):
        self.assertEqual(doctor.check_python((3, 12, 3)).status, OK)
        c = doctor.check_python((3, 8, 10))
        self.assertEqual(c.status, FAIL)
        self.assertTrue(c.hint)

    def test_module_missing_wrong_pin_ok(self):
        self.assertEqual(doctor.check_module("empy", "build", pin="3.3.4", installed="3.3.4").status, OK)
        c = doctor.check_module("empy", "build", pin="3.3.4", installed="4.2")
        self.assertEqual(c.status, FAIL)
        self.assertIn("empy==3.3.4", c.hint)
        c = doctor.check_module("pexpect", "build", installed=None)
        self.assertEqual((c.status, c.hint), (FAIL, "pip install 'pexpect'"))
        # optional modules only warn
        self.assertEqual(doctor.check_module("numpy", "autotest", required=False, installed=None).status, WARN)

    def test_python_deps_use_installed_versions(self):
        names = [c.name for c in doctor.python_deps()]
        self.assertTrue(any(n.startswith("empy") for n in names))

    def test_submodule_status(self):
        out = ("-1111111111111111111111111111111111111111 modules/mavlink\n"
               " 2222222222222222222222222222222222222222 modules/waf (heads/master)\n"
               "+3333333333333333333333333333333333333333 modules/ChibiOS (x)\n")
        self.assertEqual(doctor.uninitialised_submodules(out), ["modules/mavlink"])
        self.assertEqual(doctor.uninitialised_submodules(""), [])

    def test_meminfo_and_jobs(self):
        self.assertAlmostEqual(doctor.meminfo_gb("MemTotal:       16384000 kB\nMemFree: 1 kB\n"), 15.625)
        self.assertIsNone(doctor.meminfo_gb("nothing"))
        self.assertEqual(doctor.suggest_build_jobs(16, 13.0), 16)
        self.assertEqual(doctor.suggest_build_jobs(16, 4.0), 5)
        self.assertEqual(doctor.suggest_build_jobs(8, 0.1), 1)
        self.assertEqual(doctor.suggest_build_jobs(8, None), 8)

    def test_resources(self):
        ok = doctor.check_resources(16, 13.0, 12, 16)
        self.assertEqual([c.status for c in ok], [OK, OK, OK])
        low = doctor.check_resources(4, 2.0, 12, 4)
        self.assertEqual(by_name(low, "flight jobs").status, WARN)
        self.assertIn("-j 4", by_name(low, "flight jobs").hint)
        self.assertEqual(by_name(low, "build jobs").status, WARN)
        self.assertIn("FP_BUILD_JOBS=2", by_name(low, "build jobs").hint)

    def test_disk(self):
        self.assertEqual(doctor.check_disk(Path("/x"), 30e9).status, OK)
        c = doctor.check_disk(Path("/x"), 2e9)
        self.assertEqual(c.status, FAIL)
        self.assertIn("FP_HOME", c.hint)

    def test_min_free_matches_investigate(self):
        from forkpilot.investigate import MIN_FREE_BYTES
        self.assertEqual(doctor.MIN_FREE_GB * 1e9, MIN_FREE_BYTES)

    def test_unshare(self):
        class R:
            def __init__(s, rc, err=""):
                s.returncode, s.stderr = rc, err
        usable = doctor.check_unshare(None, run=lambda *a, **k: R(0))
        self.assertEqual(usable.status, OK)
        broken = doctor.check_unshare(None, run=lambda *a, **k: R(1, "unshare: Operation not permitted"))
        self.assertEqual(broken.status, WARN)
        self.assertIn("Operation not permitted", broken.detail)

        def missing(*a, **k):
            raise FileNotFoundError("unshare")
        self.assertEqual(doctor.check_unshare(None, run=missing).status, WARN)

    def test_render_and_summary(self):
        text = doctor.render([Check(OK, "git", "2.43"), Check(FAIL, "empy", "4.2", "pip install 'empy==3.3.4'"),
                              Check(WARN, "ccache", "not found", "sudo apt install ccache")])
        self.assertIn("[ OK ] git: 2.43", text)
        self.assertIn("[FAIL] empy: 4.2\n       fix: pip install 'empy==3.3.4'", text)
        self.assertIn("1 ok, 1 warning(s), 1 failed", text)
        self.assertNotIn("fix:", doctor.render([Check(OK, "git", "x", "unused hint")]))


class RepoChecks(unittest.TestCase):
    def test_missing_and_not_a_repo(self):
        c = doctor.check_repo(Path("/nonexistent/forkpilot-test"))
        self.assertEqual([x.status for x in c], [FAIL])
        d = Path(tempfile.mkdtemp())
        c = doctor.check_repo(d)
        self.assertEqual(c[0].status, FAIL)
        self.assertIn("not a git repository", c[0].detail)

    def test_healthy_repo(self):
        d, first, last = fake_ardupilot()
        c = doctor.check_repo(d, first, last)
        self.assertEqual([x.name for x in c if x.status != OK], [], c)
        self.assertEqual(by_name(c, "good is an ancestor").status, OK)

    def test_swapped_and_unknown_commits(self):
        d, first, last = fake_ardupilot()
        self.assertEqual(by_name(doctor.check_repo(d, last, first), "good is an ancestor").status, WARN)
        c = doctor.check_repo(d, "deadbeef", last)
        self.assertEqual(by_name(c, "good commit").status, FAIL)
        self.assertFalse(any(x.name.startswith("good is") for x in c))

    def test_not_ardupilot_dirty_tree_missing_submodule_dir(self):
        d, _, _ = fake_ardupilot()
        (d / "ArduCopter" / "Copter.cpp").write_text("// local edit\n")
        self.assertEqual(by_name(doctor.check_repo(d), "working tree").status, FAIL)
        (d / "waf").chmod(0o644)
        self.assertEqual(by_name(doctor.check_repo(d), "waf").status, FAIL)
        (d / "waf").chmod(0o755)
        for p in (d / "modules" / "waf" / "waflib").iterdir():
            p.unlink()
        (d / "modules" / "waf" / "waflib").rmdir()
        c = by_name(doctor.check_repo(d), "submodules")
        self.assertEqual(c.status, FAIL)
        self.assertIn("modules/waf", c.detail)
        self.assertIn("git submodule update --init --recursive", c.hint)


class Setup(unittest.TestCase):
    def run_py(self, code, **env):
        e = {k: v for k, v in os.environ.items() if k != "FP_HOME"}
        e.update(env, PYTHONPATH=str(ROOT))
        return subprocess.run([sys.executable, "-c", code], env=e, capture_output=True, text=True,
                              cwd=tempfile.gettempdir())

    def test_doctor_does_not_need_the_flight_stack(self):
        r = self.run_py("import sys; from forkpilot import cli, doctor; doctor.run_checks(None);"
                        "sys.exit(0 if 'pymavlink' not in sys.modules else 1)")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cli_doctor_exit_code(self):
        d = Path(tempfile.mkdtemp())
        r = self.run_py("from forkpilot.cli import main; main(['doctor', '--repo', %r])" % str(d),
                        FP_HOME=str(d / "home"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("[FAIL] ArduPilot checkout", r.stdout)
        self.assertIn("fix: clone your fork", r.stdout)


if __name__ == "__main__":
    unittest.main()
