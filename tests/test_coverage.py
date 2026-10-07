"""Coverage of a commit range: diff parsing, waf flags, and the run / not run / not built split."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from forkpilot import coverage


def sh(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class ChangedLinesTest(unittest.TestCase):
    def test_added_changed_and_deleted(self):
        repo = Path(tempfile.mkdtemp())
        sh(repo, "init", "-q")
        sh(repo, "config", "user.email", "t@t")
        sh(repo, "config", "user.name", "t")
        (repo / "a.cpp").write_text("".join(f"l{i}\n" for i in range(1, 11)))
        (repo / "notes.md").write_text("x\n")
        sh(repo, "add", ".")
        sh(repo, "commit", "-qm", "good")
        lines = [f"l{i}\n" for i in range(1, 11)]
        lines[2] = "changed\n"           # line 3
        del lines[6]                     # old line 7: a deletion only
        lines.insert(8, "new\n")         # line 9 of the new file
        (repo / "a.cpp").write_text("".join(lines))
        (repo / "b.h").write_text("one\ntwo\n")
        (repo / "notes.md").write_text("y\n")
        sh(repo, "add", ".")
        sh(repo, "commit", "-qm", "bad")
        files, deleted = coverage.changed_lines(repo, "HEAD~1", "HEAD")
        self.assertEqual(files, {"a.cpp": {3, 9}, "b.h": {1, 2}})
        self.assertEqual(deleted, 1)


class InstrumentTest(unittest.TestCase):
    def test_flags_added_once(self):
        cache = Path(tempfile.mkdtemp()) / "sitl_cache.py"
        cache.write_text("CXXFLAGS = ['-std=gnu++11']\nLINKFLAGS = ['-pthread']\nDEST_OS = 'linux'\n")
        coverage.instrument(cache)
        coverage.instrument(cache)
        text = cache.read_text()
        self.assertIn("CXXFLAGS = ['-fprofile-arcs', '-ftest-coverage', '-std=gnu++11']", text)
        self.assertIn("LINKFLAGS = ['-lgcov', '-coverage', '-pthread']", text)
        self.assertIn("DEST_OS = 'linux'", text)


class CompareTest(unittest.TestCase):
    def test_split(self):
        changed = {"ArduCopter/mode_poshold.cpp": {10, 11, 12, 13}, "ArduPlane/Attitude.cpp": {5}}
        records = {
            "pilot_sticks": {"ok": True, "hit": {"ArduCopter/mode_poshold.cpp": [10, 11]},
                             "executable": {"ArduCopter/mode_poshold.cpp": [10, 11, 12, 40]}},
            "hover": {"ok": False, "hit": {}, "executable": {"ArduCopter/mode_poshold.cpp": [10, 11, 12, 40]}},
        }
        res = coverage.compare(changed, records)
        f = res["files"]["ArduCopter/mode_poshold.cpp"]
        # line 13 has no code: not counted
        self.assertEqual((f["code"], f["run"], f["not_run"]), (3, 2, [12]))
        self.assertEqual(f["by_scenario"], {"pilot_sticks": 2})
        # hover ran nothing, so every line pilot_sticks ran is its own
        self.assertEqual(f["by_scenario_distinct"], {"pilot_sticks": 2})
        self.assertEqual(res["not_built"], ["ArduPlane/Attitude.cpp"])
        self.assertEqual((res["code_lines"], res["run_lines"]), (3, 2))
        self.assertEqual(res["failed"], ["hover"])
        text = "\n".join(coverage.summary_lines({**res, "deleted_only": 0}))
        self.assertIn("run in flight: 2 (67%)", text)
        self.assertIn("mode_poshold.cpp: 12", text)

    def test_ranges(self):
        self.assertEqual(coverage.ranges([3, 4, 5, 9, 11, 12]), "3-5, 9, 11-12")
        self.assertEqual(coverage.ranges([]), "")


class ReportCoverageTest(unittest.TestCase):
    def test_section_and_summary(self):
        from forkpilot import i18n, report
        from tests.test_report import check_html, make_inv
        root = Path(tempfile.mkdtemp())
        inv = make_inv(root)
        changed = {"ArduCopter/mode_poshold.cpp": {10, 11, 12}, "ArduPlane/Attitude.cpp": {5}}
        records = {"pilot_sticks": {"ok": True, "hit": {"ArduCopter/mode_poshold.cpp": [10, 11]},
                                    "executable": {"ArduCopter/mode_poshold.cpp": [10, 11, 12]}}}
        res = {"good": "a" * 40, "bad": "d" * 40, "deleted_only": 0, **coverage.compare(changed, records)}
        (inv / "coverage.json").write_text(json.dumps(res))
        rec = json.loads((inv / "record.json").read_text())
        rec["coverage"] = coverage.brief(res)
        (inv / "record.json").write_text(json.dumps(rec))
        self.addCleanup(lambda: (setattr(i18n, "_lang", None), os.environ.pop("FP_LANG", None)))
        for lang in ("en", "tr"):
            i18n.set_lang(lang)
            html = report.build(inv)
            check_html(self, html)
            self.assertIn("2/3", html)
            self.assertIn("ArduPlane/Attitude.cpp", html)
            self.assertIn(i18n.t("report.coverage.value", run=2, code=3, pct="67%"), html)
        # an investigation without coverage has no section
        (inv / "coverage.json").unlink()
        self.assertNotIn(i18n.t("report.h.coverage"), report.build(inv))


class NightlySummaryTest(unittest.TestCase):
    def test_line(self):
        from forkpilot.nightly import _summary
        r = {"vehicle": "copter", "good": "a" * 40, "bad": "b" * 40, "commits": 147, "first_run": False,
             "outcome": "no_regression", "wall_s": 1.0, "coverage": {"run": 211, "code": 1663}}
        self.assertIn("changed lines run in flight: 211 of 1663 (13%)", _summary("2026-10-06", "master", [r]))


if __name__ == "__main__":
    unittest.main()
