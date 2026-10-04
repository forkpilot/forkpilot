# Contributing

Bug reports, scenarios and fixes are welcome. Open an issue first for a large change.

## Set up

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m forkpilot.cli doctor
```

`doctor` checks the tools that a SITL build needs. See [docs/quickstart.md](docs/quickstart.md).

## Before you open a pull request

```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m forkpilot.cli lint
```

Both must pass. CI runs the same two commands. Unit tests do not fly SITL. If your change
alters flight behaviour, metrics or the oracle, also fly the affected scenarios and put the
numbers in the pull request.

## Rules

- The oracle decides every verdict. A language model may explain or propose, never decide.
- A new metric needs a noise floor or an A/A run that shows its spread (see `FLOORS` in
  `forkpilot/plane_metrics.py` and `forkpilot/px4_metrics.py`).
- `tests/test_suites.py` pins the scenario fingerprints. Change them only on purpose: a new
  fingerprint makes every cached baseline fly again.
- A benchmark score always comes with its caveats. A scenario written after a case was
  studied is marked post-hoc in its comments. Holdout cases are not used to tune the method.
- User-facing text goes in `forkpilot/i18n.py`, in English and Turkish.

## Scenarios

The scenario format is in [docs/scenarios.md](docs/scenarios.md). `forkpilot lint` checks a
scenario directory; `--scenarios DIR` flies it.

## License

By contributing, you agree that your contribution is licensed under the Apache License 2.0.
Files in `bench/real/patches/` are changes to ArduPilot and stay GPL-3.0.
