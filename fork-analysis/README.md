# Fork analysis

Scripts that measure public forks of ArduPilot and PX4 on GitHub, and check whether a fork
still carries the code of a known upstream bug that upstream has since fixed.

The data files (`data/*.json`) are not in this repository. They name individual forks and
their owners. Run the scripts to make your own copy.

| Script | What it does | Output |
|---|---|---|
| `fetch_forks.py` | Lists every direct fork of both upstreams (GitHub API) | `data/forks_<repo>.json` |
| `compare_forks.py` | Compares active organization forks (and forks with stars or forks of their own) with upstream: own commits, missing upstream commits, merge-base date | `data/compare_<repo>.json` |
| `refine.py` | Compares a fork that tracks a release branch with that branch, not with master | adds `custom_commits` |
| `analyze.py` | Prints a summary of the comparison | stdout |
| `known_bugs.py` | `verify`: checks each bug signature against the upstream checkout. `scan`: looks for the signatures in the forks' default branches, one raw file per check | `data/known_bugs_scan*.json` |

Requirements: `pip install requests`, an authenticated GitHub CLI (`gh auth login`; the scripts
use `gh auth token`), and for `known_bugs.py verify` an ArduPilot checkout in `ardupilot/` or a
PX4 checkout in `px4/`. Only public repositories and files are read. Nothing is cloned or run.

A hit means "the line that the bug added is in this fork's default branch". It does not mean
that the vehicle misbehaves. The fork may not use that mode, may change the code elsewhere, or
the bug may need a non-default parameter.

## Results (2026-10-03 and 2026-10-04)

Forks with at least 5 own commits, pushed after June 2025, most own commits first:

| Upstream | Signatures | Forks scanned | Carry ≥ 1 | Organization forks | Organization forks that carry ≥ 1 |
|---|---|---|---|---|---|
| ArduPilot | 19 | 198 | 135 | 48 | 30 |
| PX4 | 14 | 201 | 175 | 71 | 59 |

The signatures, with their culprit and fix commits, are the `BUGS` and `PX4_BUGS` lists in
`known_bugs.py`.
