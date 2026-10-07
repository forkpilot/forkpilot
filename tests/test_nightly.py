"""Nightly: range planning and the rules for advancing the state, with git and investigate mocked."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forkpilot import nightly

BAD = "b" * 40
OLD = "a" * 40
FIRST = "f" * 40


class Rec:
    def __init__(self, outcome, **kw):
        self.data = {"outcome": outcome, **kw}
        self.dir = Path("/inv/x")
        self.path = self.dir / "record.json"


class NightlyTest(unittest.TestCase):
    def setUp(self):
        self.n, self.dirty, self.calls, self.ancestor = 5, "", [], True
        self.work = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(nightly, "WORK", self.work),
                  mock.patch.object(nightly, "git", self.git),
                  mock.patch.object(nightly, "resolve", self.resolve),
                  mock.patch.object(nightly, "commits_between", lambda r, g, b: ["c"] * self.n)):
            p.start()
            self.addCleanup(p.stop)

    def git(self, repo, *args):
        self.calls.append(args)
        if args[0] == "status":
            return self.dirty
        if args[:2] == ("merge-base", "--is-ancestor") and not self.ancestor:
            raise subprocess.CalledProcessError(1, "git")
        return ""

    def resolve(self, repo, ref):
        return {"origin/master": BAD, f"{BAD}~20": FIRST}[ref]

    def run_(self, **kw):
        kw.setdefault("vehicles", ("copter",))
        return nightly.run(Path(tempfile.mkdtemp()), log=lambda *_: None, **kw)

    def state(self):
        return json.loads((self.work / "nightly" / "state.json").read_text())

    def seed(self, sha):
        nightly.save_state(self.work / "nightly" / "state.json",
                           {"master": {"copter": {"scenarios": {"sha": sha, "date": "x"}}}})

    def test_first_run_uses_first_range_and_advances(self):
        with mock.patch.object(nightly, "_investigate", return_value=Rec("no_regression")) as inv:
            r = self.run_()
        self.assertEqual(inv.call_args.args[1:], (FIRST, BAD))
        self.assertTrue(r[0]["first_run"])
        self.assertEqual(self.state()["master"]["copter"]["scenarios"]["sha"], BAD)
        day = self.work / "nightly" / r[0]["day"]
        self.assertTrue((day / "copter.json").exists() and (day / "summary.md").exists())
        lines = (self.work / "nightly" / "log.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["outcome"], "no_regression")

    def test_later_run_starts_at_last_checked(self):
        self.seed(OLD)
        with mock.patch.object(nightly, "_investigate", return_value=Rec("localized")) as inv:
            self.run_()
        self.assertEqual(inv.call_args.args[1], OLD)

    def test_no_new_commits(self):
        self.seed(BAD)
        with mock.patch.object(nightly, "_investigate") as inv:
            r = self.run_()
        inv.assert_not_called()
        self.assertEqual(r[0]["outcome"], "no_new_commits")

    def test_error_keeps_state_and_other_vehicle_continues(self):
        self.seed(OLD)
        outs = [RuntimeError("boom"), Rec("localized", culprit={"culprit": "c" * 40, "subject": "s"})]
        with mock.patch.object(nightly, "_investigate", side_effect=outs):
            r = self.run_(vehicles=("copter", "plane"))
        self.assertEqual([x["outcome"] for x in r], ["error", "localized"])
        self.assertIn("boom", r[0]["error"])
        st = self.state()["master"]
        self.assertEqual(st["copter"]["scenarios"]["sha"], OLD)
        self.assertEqual(st["plane"]["scenarios"]["sha"], BAD)
        summary = (self.work / "nightly" / r[0]["day"] / "summary.md").read_text()
        self.assertIn("culprit: cccccccccc s", summary)

    def test_missing_outcome_is_an_error(self):
        with mock.patch.object(nightly, "_investigate", return_value=Rec(None)):
            r = self.run_()
        self.assertEqual(r[0]["outcome"], "error")
        self.assertFalse((self.work / "nightly" / "state.json").exists())

    def test_long_range_still_investigated(self):
        self.n = 200
        with mock.patch.object(nightly, "_investigate", return_value=Rec("no_regression")) as inv:
            r = self.run_(max_commits=60)
        inv.assert_called_once()
        self.assertEqual(r[0]["commits"], 200)

    def test_dry_run_writes_nothing(self):
        with mock.patch.object(nightly, "_investigate") as inv:
            r = self.run_(dry_run=True)
        inv.assert_not_called()
        self.assertEqual(r[0]["outcome"], "dry_run")
        self.assertFalse((self.work / "nightly").exists())

    def test_force_push_restarts_from_first_range(self):
        self.seed(OLD)
        self.ancestor = False
        with mock.patch.object(nightly, "_investigate", return_value=Rec("no_regression")) as inv:
            self.run_()
        self.assertEqual(inv.call_args.args[1], FIRST)

    def test_dirty_tree_refused(self):
        self.dirty = " M file"
        with self.assertRaises(RuntimeError):
            self.run_()
        self.assertNotIn(("fetch", "origin", "master"), self.calls)


if __name__ == "__main__":
    unittest.main()


class BackfillTest(unittest.TestCase):
    LOG = "\n".join(["c1 2026-09-01", "c2 2026-09-01", "c3 2026-09-02", "c4 2026-09-04"])

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.invs = []
        for p in (mock.patch.object(nightly, "WORK", self.work),
                  mock.patch.object(nightly, "git", self.git),
                  mock.patch.object(nightly, "commits_between", lambda r, g, b: ["c"]),
                  mock.patch.object(nightly, "_investigate", self.investigate),
                  # the walk stops on low disk: keep the test independent of this machine's disk
                  mock.patch.object(nightly.shutil, "disk_usage",
                                    lambda p: mock.Mock(free=nightly.MIN_FREE_BACKFILL * 2))):
            p.start()
            self.addCleanup(p.stop)

    def git(self, repo, *args):
        if args[0] == "log":
            return self.LOG
        if args[0] == "rev-list":
            return "c0\n"
        return ""

    def investigate(self, repo, good, bad, **kw):
        self.invs.append((good, bad, kw["vehicle"]))
        d = self.work / "inv" / f"{good}-{bad}"
        (d / "bad").mkdir(parents=True)
        (d / "bad" / "hover.0.run.json.gz").write_text("x")
        (d / "bad" / "hover.0.metrics.json").write_text("{}")
        base = self.work / "base" / good
        base.mkdir(parents=True)
        rec = Rec("localized" if bad == "c3" else "no_regression", baseline_dir=str(base))
        rec.dir = d
        return rec

    def test_day_points_last_commit_per_day(self):
        pts = nightly.day_points(Path("/r"), "origin/master", "2026-09-01")
        self.assertEqual(pts, [("start", "c0"), ("2026-09-01", "c2"), ("2026-09-02", "c3"), ("2026-09-04", "c4")])
        self.assertEqual([s for _, s in nightly.day_points(Path("/r"), "m", "2026-09-01", step_days=2)],
                         ["c0", "c3"])

    def test_walk_prunes_resumes(self):
        res = nightly.backfill(Path("/r"), vehicles=("copter",), since="2026-09-01", log=lambda *a: None)
        self.assertEqual([(r["good"], r["bad"]) for r in res], [("c0", "c2"), ("c2", "c3"), ("c3", "c4")])
        # no regression: raw telemetry and the baseline go, metrics stay; localized keeps everything
        self.assertFalse((self.work / "inv" / "c0-c2" / "bad" / "hover.0.run.json.gz").exists())
        self.assertTrue((self.work / "inv" / "c0-c2" / "bad" / "hover.0.metrics.json").exists())
        self.assertFalse((self.work / "base" / "c0").exists())
        self.assertTrue((self.work / "inv" / "c2-c3" / "bad" / "hover.0.run.json.gz").exists())
        self.invs.clear()
        self.assertEqual(nightly.backfill(Path("/r"), since="2026-09-01", log=lambda *a: None), [])
        self.assertEqual(self.invs, [])
