import gzip
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from forkpilot import i18n, plots, report

DIFF = """commit %s
Author: Ada Lovelace <ada@example.com>
Date:   Sat Aug 9 12:24:12 2025 +0930

    Copter: Convert PosHold to meters

diff --git a/ArduCopter/mode_poshold.cpp b/ArduCopter/mode_poshold.cpp
--- a/ArduCopter/mode_poshold.cpp
+++ b/ArduCopter/mode_poshold.cpp
@@ -1,2 +1,2 @@
-    brake_gain = 15.0f;
+    brake_gain = 0.15f;
"""


def fake_run(coast: float) -> dict:
    """Arms at t=10, releases the sticks at t=20 flying 5 m/s, coasts `coast` m."""
    samples = []
    for i in range(0, 400):
        t = 10 + i * 0.1
        since = t - 20
        x = 0.0 if since < 0 else coast * (1 - math.exp(-since))
        vx = 0.0 if since < 0 else coast * math.exp(-since)
        samples.append({"mavpackettype": "LOCAL_POSITION_NED", "t": t, "x": x, "y": 0.0, "z": -10.0,
                        "vx": vx, "vy": 0.0, "vz": 0.0})
    ev = [[10.0, "armed", None], [18.0, "mode", "POSHOLD"], [20.0, "release", "POSHOLD"],
          [35.0, "release_end", "POSHOLD"], [49.0, "disarmed", None]]
    return {"scenario": "pilot_sticks", "events": ev, "samples": samples, "ok": True, "error": None}


def put_runs(d: Path, coasts, reps=None):
    d.mkdir(parents=True, exist_ok=True)
    for rep, c in zip(reps or range(len(coasts)), coasts):
        (d / f"pilot_sticks.{rep}.run.json.gz").write_bytes(gzip.compress(json.dumps(fake_run(c)).encode()))
        (d / f"pilot_sticks.{rep}.metrics.json").write_text(json.dumps({"stop_dist_m_poshold": c, "completed": 1.0}))


def make_inv(root: Path, *, with_files=True) -> Path:
    inv = root / "20261004-000001"
    inv.mkdir()
    culprit = "d" * 40
    base = root / "baseline"
    finding = "DRIFT stop_dist_m_poshold: 5.000 (beklenen [8.000, 10.000])"
    rec = {"started": "20261004-000001", "repo": "/nonexistent", "good": "a" * 40, "bad": culprit,
           "suite": "scenarios", "baseline_dir": str(base), "outcome": "localized", "targets": ["pilot_sticks"],
           "symptom": {"pilot_sticks": "DRIFT"}, "wanted": [["pilot_sticks", "stop_dist_m_poshold", "drift", "-"]],
           "culprit": {"culprit": culprit, "subject": "Copter: Convert PosHold to meters",
                       "files": ["ArduCopter/mode_poshold.cpp"], "tests": 2, "range": 4, "ambiguous_with": []},
           "steps": [
               {"kind": "detect", "at_s": 60.0, "worst": "DRIFT", "soaked": [], "report": {
                   "pilot_sticks": {"verdict": "DRIFT", "findings": [finding]},
                   "hover": {"verdict": "PASS", "findings": []}}},
               {"kind": "triage", "at_s": 70.0, "scenarios": {"pilot_sticks": {
                   "class": "consistent", "non_pass": 3, "runs": 3}}},
               {"kind": "timeline", "at_s": 71.0, "scenario": "pilot_sticks", "first_divergence_t": 12.0,
                "signals": [{"signal": "yatay konum", "t": 12.0, "baseline": "(K 1, D 2)",
                             "candidate": "(K 1, D 3)", "outside_m": 0.4}]},
               {"kind": "bisect_test", "at_s": 80.0, "sha": "b" * 40, "result": "good", "runs": 2,
                "verdict": {}, "build": {"cached": True, "seconds": 0.0}},
               {"kind": "bisect_test", "at_s": 85.0, "sha": "c" * 40, "result": "skip", "build":
                {"cached": False, "seconds": 3.0, "error": "x <script>alert(1)</script>"}},
               {"kind": "bisect_test", "at_s": 90.0, "sha": culprit, "result": "bad", "runs": 2,
                "verdict": {"pilot_sticks": {"verdict": "DRIFT", "findings": [finding]}},
                "build": {"cached": False, "seconds": 20.0}}]}
    (inv / "record.json").write_text(json.dumps(rec))
    if with_files:
        (inv / "evidence.md").write_text("# Evidence\n\n```diff\n" + DIFF % culprit + "```\n")
        put_runs(base, [9.0, 9.2, 9.4, 9.1, 9.3])
        put_runs(inv / "bad", [5.0, 5.1, 5.2])
        (inv / "explanation.json").write_text(json.dumps({
            "backend": "local", "model": "m", "lang": "en", "summary": "Units changed.",
            "mechanism": "Gain is 100x lower.", "affected_situations": ["PosHold stop"],
            "confidence": "medium", "open_questions": ["Other modes?"],
            "culprit_lines": [{"file": "a.cpp", "quote": "+x", "why": "w", "verified": True},
                              {"file": "b.cpp", "quote": "+invented", "why": "w", "verified": False}]}))
        (inv / "fix.json").write_text(json.dumps({"candidates": [
            {"name": "revert", "source": "git revert", "outcome": "fixes", "diff_lines": 9, "sha": "e" * 40},
            {"name": "llm-1", "source": "local:m", "outcome": "partial", "remaining": [["pilot_sticks", "m", "drift", "+"]],
             "rationale": "why", "risk": "risk"}]}))
        (inv / "fix").mkdir()
        (inv / "fix" / "revert.diff").write_text("diff --git a/x b/x\n-old\n+new\n")
    return inv


class Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.depth = [], [], 0
        self.stack, self.css, self.in_style = [], "", False

    def handle_data(self, data):
        if self.stack and self.stack[-1] == "style":
            self.css += data

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += attrs
        if tag not in ("meta", "link", "br", "img", "input", "hr", "path", "line", "rect", "circle", "polyline", "polygon"):
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += attrs

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"unbalanced </{tag}> after {self.stack[-3:]}"
        self.stack.pop()


def check_html(test: unittest.TestCase, html: str) -> Collector:
    c = Collector()
    c.feed(html)
    c.close()
    test.assertEqual(c.stack, [])
    for forbidden in ("script", "link", "img", "iframe", "object", "embed", "form"):
        test.assertNotIn(forbidden, c.tags)
    # request-capable places only: a diff may legitimately contain a URL as text
    test.assertFalse(re.search(r"https?:|//[a-z]|@import|url\(", c.css), "external reference in CSS")
    for name, value in c.attrs:
        test.assertNotIn(name, ("src", "href", "srcset", "action", "data", "poster"))
        test.assertFalse(re.search(r"https?:|^//", value or ""), "external reference in attribute")
    test.assertIn("prefers-color-scheme:dark", html)
    return c


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def render(self, inv, lang="en"):
        i18n.set_lang(lang)
        self.addCleanup(i18n.set_lang, "en")
        return report.build(inv)

    def test_full_report(self):
        html = self.render(make_inv(self.root))
        check_html(self, html)
        order = [html.index(x) for x in ("LOCALIZED", "Verdict per scenario", "Telemetry", "Bisect trail",
                                         "Culprit diff", "Explanation", "Fix candidates", "does not replace flight tests")]
        self.assertEqual(order, sorted(order))
        self.assertIn("Ada Lovelace", html)
        self.assertIn("mode_poshold.cpp", html)
        self.assertEqual(html.count("<svg"), 3)             # zoom, whole flight, band chart
        self.assertIn("below band", html)
        self.assertIn("unverified", html)                    # the unverified quote stays marked
        self.assertIn("&lt;script&gt;", html)                # build logs are escaped
        self.assertIn("brake_gain = 0.15f", html)
        self.assertIn("fixes", html)
        self.assertIn("<code>revert</code>", html)

    def test_turkish_and_english_differ_but_both_valid(self):
        inv = make_inv(self.root)
        en, tr = self.render(inv, "en"), self.render(inv, "tr")
        check_html(self, tr)
        self.assertIn('lang="tr"', tr)
        self.assertIn("Uçuş testinin yerini tutmaz", tr)
        self.assertIn("bandın altında", tr)
        self.assertNotIn("bandın altında", en)

    def test_partial_investigations_never_crash(self):
        inv = make_inv(self.root, with_files=False)
        html = self.render(inv)
        check_html(self, html)
        self.assertIn("LOCALIZED", html)
        self.assertNotIn("Culprit diff", html)
        self.assertNotIn("Explanation", html)
        self.assertNotIn("Fix candidates", html)
        self.assertIn("No telemetry was kept", html)        # numbers only, no plot
        rec = json.loads((inv / "record.json").read_text())
        for outcome, steps in (("build_failed", []), (None, []), ("no_regression", rec["steps"][:1])):
            rec2 = {k: v for k, v in rec.items() if k not in ("culprit", "targets", "wanted", "symptom")}
            rec2.update(outcome=outcome, steps=steps)
            (inv / "record.json").write_text(json.dumps(rec2))
            check_html(self, self.render(inv))
        (inv / "record.json").write_text(json.dumps({"started": "x"}))
        self.assertIn("INCOMPLETE", self.render(inv))

    def test_missing_or_broken_record(self):
        with self.assertRaises(report.ReportError):
            report.build(self.root)
        (self.root / "record.json").write_text("{broken")
        with self.assertRaises(report.ReportError):
            report.build(self.root)

    def test_write_default_location(self):
        inv = make_inv(self.root)
        self.assertEqual(report.write(inv), inv / "report.html")
        self.assertTrue((inv / "report.html").read_text().startswith("<!doctype html>"))

    def test_cli_language_flag_and_env(self):
        inv = make_inv(self.root)
        root = Path(__file__).resolve().parent.parent

        def run(args, **env):
            e = {k: v for k, v in os.environ.items() if k != "FP_LANG"} | env
            p = subprocess.run([sys.executable, "-m", "forkpilot.cli", *args], cwd=root, env=e,
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout, (inv / "report.html").read_text()

        out, html = run(["report", str(inv), "--out", str(inv / "report.html")])
        self.assertTrue(out.startswith("report: "))
        self.assertIn('lang="en"', html)
        out, html = run(["--lang", "tr", "report", str(inv), "--out", str(inv / "report.html")])
        self.assertTrue(out.startswith("rapor: "))
        self.assertIn('lang="tr"', html)
        out, html = run(["report", str(inv), "--out", str(inv / "report.html")], FP_LANG="tr")
        self.assertIn('lang="tr"', html)

    def test_parse_finding_both_languages(self):
        for word in ("beklenen", "expected"):
            f = report.parse_finding(f"DRIFT stop_dist_m_x: 7.725 ({word} [8.663, 10.588])")
            self.assertEqual((f["metric"], f["kind"], f["lo"], f["hi"], f["side"]),
                             ("stop_dist_m_x", "drift", 8.663, 10.588, "-"))
            f = report.parse_finding(f"RULE  range_max_m: 82.4 ({word} lt 60)")
            self.assertEqual((f["kind"], f["rule"]), ("rule", ("lt", 60.0)))
        f = report.parse_finding({"metric": "m", "kind": "drift", "value": 2.0, "expected": "[0.0, 1.0]", "side": "+"})
        self.assertEqual((f["hi"], f["side"]), (1.0, "+"))
        self.assertEqual(report.parse_finding("garbage")["metric"], "garbage")

    def test_md_fallback_keeps_markers(self):
        html = report.md_html("## Lines\n\n- `a.cpp` **[unverified]**\n  ```\n  code <x>\n  ```\n")
        self.assertIn('<strong class="warn">[unverified]</strong>', html)
        self.assertIn("code &lt;x&gt;", html)


class PlotTest(unittest.TestCase):
    def test_ticks(self):
        self.assertEqual(plots.nice_ticks(0, 10, 5), [0, 2, 4, 6, 8, 10])
        self.assertEqual(plots.nice_ticks(0.05, 0.31, 4)[0], 0.1)

    def test_units(self):
        self.assertEqual(plots.unit_of("stop_dist_m_poshold"), "m")
        self.assertEqual(plots.unit_of("mode_speed_mps_poshold2"), "m/s")
        self.assertEqual(plots.unit_of("armed_time_s"), "s")
        self.assertEqual(plots.unit_of("mission_wp_reached"), "")

    def test_resample(self):
        self.assertEqual(plots.resample([0, 2], [0, 4], [-1, 0, 1, 2, 3]), [None, 0, 2.0, 4, None])

    def test_figure_to_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            put_runs(tmp / "good", [9.0, 9.2])
            put_runs(tmp / "bad", [5.0])
            fig = plots.telemetry_figure("stop_dist_m_poshold", plots.run_files(tmp / "good", "pilot_sticks"),
                                         plots.run_files(tmp / "bad", "pilot_sticks"), 12.0)
            self.assertEqual((fig["good"], fig["bad"]), (2, 1))
            self.assertIn("Distance from release point (m)", fig["zoom"])
            self.assertIn("Time since stick release (s)", fig["zoom"])
            # the plotted maximum of the bad run is its 5 m coast, the good runs' about 9 m
            self.assertIsNone(plots.telemetry_figure("x", [], []))


if __name__ == "__main__":
    unittest.main()
