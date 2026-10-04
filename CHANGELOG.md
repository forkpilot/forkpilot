# Changelog

## 0.1.0 (unreleased)

First public release.

- `investigate`: detect, triage, bisect and evidence for ArduCopter, ArduPlane, QuadPlane
  and PX4 multicopter (SIH).
- Deterministic oracle: PASS, DRIFT or FAIL against a baseline band, with per-metric noise
  floors. Drifts in noisy suites are confirmed on fresh runs (Mann-Whitney).
- Suites: ForkPilot scenarios (`scenarios/`) and ArduPilot's own autotest.
- `explain` and `fix`: optional language model (Anthropic or a local OpenAI-compatible
  server). The oracle judges every fix candidate by flying it.
- `report`: one self-contained HTML file, no scripts and no network requests.
- `fromlog` and `replaycheck`: a scenario from a flight log.
- `doctor`, `lint`, `--scenarios DIR`, English and Turkish output (`--lang`).
- Benchmarks: synthetic fork regressions, real ArduPilot regressions (dev and holdout cases).
