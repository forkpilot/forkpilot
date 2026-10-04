"""Offline tests for the fix stage (git plumbing, edit application, prompts). No builds, no flights."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from forkpilot import fix as fx
from forkpilot.explain import ExplainError


def sh(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def make_repo():
    """main.cpp: commit A (good), B (culprit: 0.5 -> 50), C (unrelated later change)."""
    d = Path(tempfile.mkdtemp())
    sh(d, "init", "-q")
    sh(d, "config", "user.email", "t@t")
    sh(d, "config", "user.name", "t")
    f = d / "main.cpp"
    f.write_text("float land_speed() {\n    return 0.5f;\n}\n\nint other() { return 1; }\n")
    sh(d, "add", "main.cpp")
    sh(d, "commit", "-qm", "A")
    f.write_text(f.read_text().replace("0.5f", "50.0f"))
    sh(d, "commit", "-qam", "B")
    f.write_text(f.read_text().replace("return 1;", "return 2;"))
    sh(d, "commit", "-qam", "C")
    return d, *sh(d, "rev-list", "--reverse", "HEAD").split()


class FixGitTest(unittest.TestCase):
    def test_revert_candidate(self):
        d, a, b, c = make_repo()
        sha = fx.commit_candidate(d, c, "revert", "t1", fx.reverter([b]))
        text = sh(d, "show", f"{sha}:main.cpp")
        self.assertIn("0.5f", text)
        self.assertIn("return 2;", text)            # later work kept
        self.assertEqual(sh(d, "rev-parse", "refs/forkpilot/fix/t1/revert"), sha)
        self.assertEqual(sh(d, "status", "--porcelain", "--untracked-files=no"), "")

    def test_submodule_pointer_not_committed(self):
        sub, a0, b0, c0 = make_repo()
        d, a, b, c = make_repo()
        sh(d, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "mod")
        sh(d, "commit", "-qm", "add submodule")
        head = sh(d, "rev-parse", "HEAD")
        sh(d / "mod", "checkout", "-q", a0)          # checkout lags behind, as after a far checkout
        sha = fx.commit_candidate(d, head, "revert", "t6", fx.reverter([b]))
        self.assertEqual(sh(d, "show", "--name-only", "--format=", sha), "main.cpp")

    def test_revert_conflict_is_unapplicable_and_clean(self):
        d, a, b, c = make_repo()
        (d / "main.cpp").write_text((d / "main.cpp").read_text().replace("50.0f", "49.0f"))
        sh(d, "commit", "-qam", "D")
        head = sh(d, "rev-parse", "HEAD")
        with self.assertRaises(fx.Unapplicable):
            fx.commit_candidate(d, head, "revert", "t2", fx.reverter([b]))
        self.assertEqual(sh(d, "rev-parse", "HEAD"), head)
        self.assertEqual(sh(d, "status", "--porcelain", "--untracked-files=no"), "")

    def test_pick_candidate(self):
        d, a, b, c = make_repo()
        sh(d, "checkout", "-q", "-b", "upstream", c)
        (d / "main.cpp").write_text((d / "main.cpp").read_text().replace("50.0f", "0.5f"))
        sh(d, "commit", "-qam", "fix")
        fixc = sh(d, "rev-parse", "HEAD")
        sh(d, "checkout", "-q", "--detach", c)
        sha = fx.commit_candidate(d, c, "pick", "t5", fx.picker([fixc]))
        self.assertIn("0.5f", sh(d, "show", f"{sha}:main.cpp"))

    def test_checkout_lock(self):
        from forkpilot.build import lock_checkout
        d, *_ = make_repo()
        held = lock_checkout(d)
        with self.assertRaises(RuntimeError):
            lock_checkout(d)
        held.close()
        lock_checkout(d).close()

    def test_patch_candidate(self):
        d, a, b, c = make_repo()
        sh(d, "checkout", "-q", "--detach", c)
        (d / "main.cpp").write_text((d / "main.cpp").read_text().replace("50.0f", "0.5f"))
        patch = Path(tempfile.mkdtemp()) / "p.diff"
        patch.write_text(sh(d, "diff") + "\n")
        sh(d, "checkout", "-q", "--", "main.cpp")
        sha = fx.commit_candidate(d, c, "patch-1", "t7", fx.patcher(patch))
        self.assertIn("return 0.5f;", sh(d, "show", f"{sha}:main.cpp"))

    def test_edit_candidate(self):
        d, a, b, c = make_repo()
        edits = [{"file": "main.cpp", "find": "    return 50.0f;", "replace": "    return 0.5f;"}]
        sha = fx.commit_candidate(d, c, "llm-1", "t3",
                                  lambda r: fx.apply_edits(r, edits, ["main.cpp"]))
        self.assertIn("return 0.5f;", sh(d, "show", f"{sha}:main.cpp"))

    def test_edit_rules(self):
        d, a, b, c = make_repo()
        sh(d, "checkout", "-q", "--detach", c)
        cases = [
            [{"file": "other.cpp", "find": "x", "replace": "y"}],          # not allowed
            [{"file": "main.cpp", "find": "return", "replace": "x"}],      # ambiguous
            [{"file": "main.cpp", "find": "nope", "replace": "x"}],        # absent
            [],                                                            # nothing
        ]
        for edits in cases:
            with self.assertRaises(fx.Unapplicable):
                fx.apply_edits(d, edits, ["main.cpp"])
        self.assertEqual(sh(d, "status", "--porcelain"), "")

    def test_noop_edit_is_unapplicable(self):
        d, a, b, c = make_repo()
        edits = [{"file": "main.cpp", "find": "return 2;", "replace": "return 2;"}]
        with self.assertRaises(fx.Unapplicable):
            fx.commit_candidate(d, c, "llm-1", "t4", lambda r: fx.apply_edits(r, edits, ["main.cpp"]))

    def test_code_context(self):
        d, a, b, c = make_repo()
        text, allowed = fx.code_context(d, c, b, ["main.cpp", "README.md"])
        self.assertEqual(allowed, ["main.cpp"])
        self.assertIn("return 2;", text)            # the file as it is at `bad`, not at the culprit

    def test_code_context_windows(self):
        d, a, b, c = make_repo()
        body = "".join(f"int f{i}() {{ return {i}; }}\n" for i in range(3000))
        (d / "big.cpp").write_text(body)
        sh(d, "add", "big.cpp")
        sh(d, "commit", "-qm", "big")
        (d / "big.cpp").write_text(body.replace("return 1500;", "return -1500;"))
        sh(d, "commit", "-qam", "culprit")
        cul = sh(d, "rev-parse", "HEAD")
        text, allowed = fx.code_context(d, cul, cul, ["big.cpp"])
        self.assertIn("return -1500;", text)
        self.assertIn("lines omitted", text)
        self.assertNotIn("f10()", text)
        self.assertLess(len(text), 10_000)


class SideEffectTest(unittest.TestCase):
    def test_holds(self):
        sig = ("hover", "x", "drift", "+")
        base = [{"x": v} for v in (0.10, 0.12, 0.11, 0.13, 0.12)]
        self.assertTrue(fx.holds(sig, [{"x": v} for v in (0.30, 0.31, 0.29, 0.32, 0.30)], base))
        # just over the band in one pass, but the fresh runs overlap the baseline
        self.assertFalse(fx.holds(sig, [{"x": v} for v in (0.12, 0.10, 0.14, 0.11, 0.13)], base))


class FixPromptTest(unittest.TestCase):
    def test_parse(self):
        ok = {"rationale": "r", "risk": "k", "edits": [{"file": "a", "find": "b", "replace": "c"}]}
        self.assertEqual(fx.parse(json.dumps(ok))["edits"][0]["file"], "a")
        with self.assertRaises(ExplainError):
            fx.parse(json.dumps({"rationale": "r", "edits": []}))
        with self.assertRaises(ExplainError):
            fx.parse(json.dumps({"rationale": "r", "risk": "k", "edits": [{"file": "a"}]}))

    def test_prompt_and_feedback(self):
        self.assertIn("Turkish", fx.system_prompt("tr"))
        self.assertIn("never decide", fx.system_prompt("en"))
        fb = fx.feedback([{"name": "llm-1", "outcome": "no_effect", "rationale": "why",
                           "edits": [{"file": "m.cpp", "find": "a\nb", "replace": "c"}],
                           "findings": {"hover": ["DRIFT x: 1.000"]}}])
        self.assertIn("llm-1: no_effect", fb)
        self.assertIn("- b", fb)
        self.assertIn("hover: DRIFT x", fb)

    def test_render(self):
        data = {"candidates": [
            {"name": "revert", "source": "git revert", "outcome": "fixes", "sha": "a" * 40, "diff_lines": 9,
             "full_battery": True},
            {"name": "llm-1", "source": "local:m", "outcome": "not_applicable", "error": "find 0 kez",
             "rationale": "neden", "risk": "risk"}]}
        md = fx.render(data, "tr")
        self.assertIn("| revert | git revert | düzeltiyor | 9 |", md)
        self.assertIn("uygulanamadı", md)
        self.assertIn("uçuş testinin yerini tutmaz", md)
        self.assertIn("**Gerekçe:** neden", md)

    def test_render_english(self):
        data = {"candidates": [
            {"name": "revert", "source": "git revert", "outcome": "side_effects", "sha": "a" * 40, "diff_lines": 9,
             "full_battery": False},
            {"name": "llm-1", "source": "local:m", "outcome": "not_applicable", "error": "find 0 times",
             "rationale": "why", "risk": "risk"}]}
        md = fx.render(data, "en")
        self.assertIn("| revert | git revert | fixes, with side effects | 9 |", md)
        self.assertIn("not applicable", md)
        self.assertIn("Simulation does not replace flight testing", md)
        self.assertIn("**Rationale:** why", md)
        self.assertIn("The full battery is flown only for the scenarios suite.", md)
        self.assertEqual(fx.render(data, "en"), fx.render(data, "en"))


if __name__ == "__main__":
    unittest.main()
