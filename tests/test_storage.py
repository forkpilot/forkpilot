"""Compacted telemetry: gzip on disk, read back the same, a fresh plain file wins."""
import json
import tempfile
import unittest
from pathlib import Path

from forkpilot.battery import compact, metrics_path, read_run, run_paths


class StorageTest(unittest.TestCase):
    def test_compact_and_read(self):
        d = Path(tempfile.mkdtemp())
        for rep in range(3):
            (d / f"hover.{rep}.run.json").write_text(json.dumps({"rep": rep, "samples": [1] * 100}))
        compact(d)
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         [f"hover.{r}.run.json.gz" for r in range(3)])
        self.assertEqual([read_run(p)["rep"] for p in run_paths(d)], [0, 1, 2])
        self.assertEqual(metrics_path(run_paths(d)[0]).name, "hover.0.metrics.json")
        (d / "hover.1.run.json").write_text(json.dumps({"rep": "new"}))     # rerun, not yet compacted
        self.assertEqual([read_run(p)["rep"] for p in run_paths(d, "hover")], [0, "new", 2])
        compact(d)
        self.assertEqual(read_run(d / "hover.1.run.json.gz")["rep"], "new")


if __name__ == "__main__":
    unittest.main()
