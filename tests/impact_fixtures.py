"""A read-only stand-in for forkpilot.impact.RepoGit that serves a fixture directory.

tests/data/impact/<case>/ holds `diff.patch` (git diff -U0 of the culprit commit) and `files/`,
the few upstream source files the analysis reads, as the culprit sees them. Files are excerpts:
lines the analysis never looks at are blank, so line numbers stay the same as upstream.
`good/` (optional) holds the files that differ before the commit. Revisions are "good" and "bad".
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

DATA = Path(__file__).parent / "data" / "impact"


class DirGit:
    def __init__(self, case: str):
        self.root = DATA / case

    def diff(self, good, bad):
        return (self.root / "diff.patch").read_text()

    def _file(self, rev, path):
        if rev == "good" and (self.root / "good" / path).is_file():
            return self.root / "good" / path
        p = self.root / "files" / path
        return p if p.is_file() else None

    def show(self, rev, path):
        p = self._file(rev, path)
        return p.read_text() if p else None

    def grep(self, pattern, rev, paths):
        rx = re.compile(pattern)
        base = self.root / "files"
        out = []
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(base).as_posix()
            if not p.is_file() or not any(fnmatch.fnmatch(rel, s) or rel.startswith(s + "/") for s in paths):
                continue
            for no, line in enumerate(self._file(rev, rel).read_text().splitlines(), 1):
                if rx.search(line):
                    out.append((rel, no, line))
        return out
