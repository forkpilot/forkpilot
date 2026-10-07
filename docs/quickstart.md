# Quickstart: your first investigation on your own fork

This takes about 15 minutes of your time, plus machine time for the first builds. Everything runs
on your machine. ForkPilot itself opens no network connection except to an LLM server, and only if
you ask for one (see [On-premises and LLM notes](#on-premises-and-llm-notes)). `git` and `pip` use
the network only as far as you let them (submodules, package mirror).

You need:

- Linux (Ubuntu 22.04 or 24.04 tested; other distributions work if ArduPilot builds on them),
  Python 3.10 or newer, 16 GB RAM and 8 or more cores recommended, 20 GB free disk.
- A clone of your ArduPilot fork with its submodules.
- Two commits of that fork: `good` (flew fine) and `bad` (shows a problem, or just the current
  tip you want to check).

## 1. Install

System packages (the same set ArduPilot's `install-prereqs-ubuntu.sh` installs for a SITL build,
without the GUI parts):

```bash
sudo apt install build-essential ccache g++ gawk git make python3-dev python3-venv \
    libtool libxml2-dev libxslt1-dev iproute2 rsync
```

ForkPilot, in its own virtual environment:

```bash
git clone <your internal copy of ForkPilot> forkpilot && cd forkpilot
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                 # add '.[autotest]' to also fly ArduPilot's own tests (--suite autotest)
```

This puts a `forkpilot` command on your path (inside the venv). It installs `pymavlink`, `PyYAML`
and the Python modules the ArduPilot build needs: `empy==3.3.4` (exactly this version, empy 4
breaks ArduPilot's waf), `pexpect`, and `setuptools<81` (older ArduPilot, such as 2025-08 master, imports
`pkg_resources`, which setuptools 81 removed). ForkPilot's own build runs with the Python of the venv you
installed it in, so keep that venv activated, or call `.venv/bin/forkpilot` directly.

No internet on the machine? On a connected one run `pip download -d wheels '.[autotest]'`, copy
`wheels/` over, then `pip install --no-index --find-links wheels -e .`. An internal PyPI mirror
works the usual way (`PIP_INDEX_URL`).

## 2. Give ForkPilot its own clone of your fork

ForkPilot checks out commits in the tree you give it, builds them there, and puts your branch
back when it finishes. It refuses to start over uncommitted changes and locks the tree while it
runs. So do not point it at the clone you work in:

```bash
git clone --recurse-submodules --jobs 8 <your fork url> ~/fp/fork   # or: --reference ~/your/clone
```

A full ArduPilot clone with its submodules is about 2 GB. From GitHub this can take 30-60 minutes
even on a fast line (measured: about 40 minutes over a 500 Mbit/s connection); `--jobs 8` fetches the
submodules in parallel. If you already have a full clone, `--reference` it and almost nothing is
downloaded again. Do not use a shallow clone (`--depth`): ForkPilot needs the commits between good and bad.

Every commit in the range may pin different submodule versions (MAVLink above all), and ForkPilot
runs `git submodule update --init --recursive` for each build. On a machine without access to the
submodule remotes, fetch all of them once while you have access (or point `.gitmodules` at your
internal mirrors), otherwise builds of older commits fail.

Choose where builds, baselines and investigations go. They grow: a build tree is about 1 GB, an
ArduPilot-autotest baseline about 2 GB.

```bash
export FP_HOME=~/fp/work
```

Without `FP_HOME` they go into the ForkPilot source tree (`builds/`, `results/`,
`investigations/`).

## 3. Run doctor

```bash
forkpilot doctor --repo ~/fp/fork --good <good sha> --bad <bad sha>
```

Every line is `OK`, `WARN` (works, but something is worth fixing) or `FAIL`, and every non-OK line
says what to do:

```text
[ OK ] Python: 3.12.3
[FAIL] empy (ArduPilot build): 4.2, need exactly 3.3.4
       fix: pip install 'empy==3.3.4'
[WARN] ccache (optional, faster rebuilds): not found
       fix: sudo apt install ccache
```

What it checks: Python and the modules above, `git`, `gcc`/`g++`, ccache (optional, it makes
rebuilds of nearby commits faster), your checkout (git repository, `waf`,
`ArduCopter/`, submodules initialised, clean tree, both commits exist), `unshare -rn` (only needed
by `--suite autotest` on ArduPilot versions without unix-socket support), free disk (an
investigation refuses to start below 5 GB), and cores and RAM against the number of parallel
builds and SITLs it will use. Fix every `FAIL`. The exit code is 1 if any check failed.

Then once, the smoke test:

```bash
FP_BUILD_JOBS=8 forkpilot doctor --repo ~/fp/fork --build
```

It builds the SITL binary for the checkout's HEAD and flies the `hover` scenario one time. The
first build of a tree takes 10 to 30 minutes depending on cores; ccache and the binary cache make
later ones fast. When this ends with `fly hover: PASS`, build and flight both work.
`FP_BUILD_JOBS` limits the compiler processes (default: all cores; each needs about 0.75 GB RAM).

## 4. First investigation

```bash
forkpilot investigate --repo ~/fp/fork --good <good sha> --bad <bad sha>
```

Add `--only hover circle` to fly just those scenarios for a first, quicker try, and `-j N` to set
the number of parallel SITL instances (default: your core count, at most 12).

What happens, in order (each step is printed with a timestamp):

1. Builds `good` and `bad` (cached by commit under `$FP_HOME/builds/cache/<sha>/`).
2. Flies the full scenario battery on `good` five times (the baseline; cached and reused by
   any later investigation with the same `good` and the same scenarios) and on `bad` three times.
3. The oracle gives each scenario PASS, DRIFT or FAIL.
4. Anything not PASS is rerun to classify it.
5. If something is consistent, it bisects the commits between `good` and `bad` to the first
   commit that shows the same symptom, building and flying each step.
6. Writes the evidence.

Time: the flights take minutes (a scenario flies at 20x speed); builds dominate. For a range of
hundreds of commits plan for an hour or more the first time, because bisect builds about
log2(range) more commits. A second investigation on the same `good` skips the baseline.
The disk space check and the lock on the checkout mean you can start it and leave.

If you know which scenario your CI or pilots saw fail, add `--reported <scenario>`: a failure that
only happens sometimes is then flown many more times before ForkPilot calls the commit clean.

## 5. Reading the output

The last line of the run names the investigation directory, `$FP_HOME/investigations/<stamp>/`:

| File | Contents |
|---|---|
| `evidence.md` | The report. Oracle findings per scenario, triage, the first moment `bad` diverges from `good` in the telemetry, the bisect result and the culprit commit's diff. Everything an LLM may see; nothing in it is generated. |
| `record.json` | Every step and its numbers, written as it happened. `outcome` is the result (below). |
| `explanation.md` | Only with `--explain`: a plain-language explanation of `evidence.md`. |
| `fix.md`, `fix/*.diff` | Only after `forkpilot fix`: candidate fixes and what flying them showed. |
| `bad/` | Telemetry of the runs on `bad`. |

`outcome` in `record.json`:

| Outcome | Meaning and next step |
|---|---|
| `localized` | Found a commit. Read `evidence.md`. |
| `no_regression` | The battery found no difference between `good` and `bad`. That says nothing about behaviours no scenario flies: add a scenario that does (section 6). |
| `not_reproducible` | Something drifted once but did not repeat. Treat as noise, or add `--reported`. |
| `build_failed` | `good` or `bad` does not build. The log is `$FP_HOME/builds/cache/<sha>/build.log`. |

Verdicts, per scenario (this is the deterministic oracle, `forkpilot/oracle.py`):

- **FAIL**: a rule in the scenario's `expect:` block is broken, or the scenario did not complete.
- **DRIFT**: no rule broken, but a metric's mean left the baseline band
  `mean ± max(3·sd, 10% of |mean|, 0.05)`.
- **PASS**: otherwise.

Triage classes for the non-PASS ones: `consistent` (repeats on `bad`: this is what gets bisected),
`intermittent` (a rule breaks in some runs and never in the baseline: also bisected, with more runs
per step), `flaky` (noise), `preexisting` (`good` breaks the same rule about as often), `infra`
(SITL did not start; not firmware behaviour), `not_reproduced`.

A culprit is the first commit on the first-parent line between `good` and `bad`. If your fork merges
upstream, a whole merge is one step, and the culprit can be that merge commit. ForkPilot cannot
tell you which upstream commit inside it did it; investigate the merged branch separately
(`--good <merge>^1 --bad <merge>^2`).

Then, optionally, ask for fix candidates (a revert of the culprit, a known fix you give with
`--pick <sha>`, a patch file with `--patch`). Each is built and flown by the same oracle:

```bash
forkpilot fix $FP_HOME/investigations/<stamp> --pick <upstream fix sha>
```

A candidate that makes the oracle happy is a candidate, not a fix: read the diff and fly it.

### Impact of a commit range, before you fly anything

```bash
python -m forkpilot.cli impact --repo ~/forkpilot-work/ardupilot --good <sha> --bad <sha>
```

This takes seconds and needs no build. It reads the diff and the ArduPilot sources from the
repository's objects only, and tells you which modes and parameters the change can reach (for
example an `AC_Loiter` change reaches LOITER, POSHOLD and ZIGZAG, and a new `LOIT_OPTIONS` bit
parameter), then which of them the shipped scenarios fly, and which they never fly or only fly at
the default value. Use it to decide what else to flight-test. It is a heuristic over names and file
layout: a mode or parameter it does not list is not proven unaffected, and "all modes" means core
code changed. Add `--vehicle plane` for Plane and QuadPlane, `--json` for machine-readable output.

`--plan` also prints the extra scenarios `investigate --targeted` would fly for this range, and why:

```bash
python -m forkpilot.cli impact --repo ~/forkpilot-work/ardupilot --good 275c54a24b^ --bad 275c54a24b --plan
...
Targeted flights added (1, at most 12):
  copter_sticks__ZIGZAG: ZIGZAG uses the changed code and no scenario flies it: generic stick template
  Parameters not varied: LOIT_OPTIONS (new: the good commit cannot set it, so no baseline)
```

`investigate --targeted` (off by default) flies the full set plus these. Rule-based, no LLM:

- **Parameter variants.** An affected parameter with documented bits gets one variant per bit,
  that bit flipped from the default; one with `@Values` gets one per value other than the default.
  The variant is a copy of a scenario that flies the modes using the changed code (else `hover`,
  or `plane_modes` for Plane) with the parameter in `params:`, named
  `<scenario>__<PARAM>_<value>`, e.g. `auto_mission__MIS_OPTIONS_4`. Numeric `@Range` parameters,
  and parameters the range adds (the good commit refuses to set them), are listed as not varied.
- **Stick templates.** An affected mode no scenario flies gets a generic template when a pilot can
  fly it in SITL with no extra hardware: Copter ACRO, DRIFT, SPORT, STABILIZE, ZIGZAG (take off,
  enter the mode, pitch, roll, pitch with yaw, each held and released, land); Plane ACRO, CRUISE,
  FBWB, STABILIZE, TRAINING (AUTO takeoff, enter the mode, full roll stick past the roll limit,
  release, pitch, release, RTL). Others (AUTO sub-modes, FLOWHOLD, THROW, ...) are listed as not
  flown.

At most 12 are added: templates of directly affected modes first, then vehicle parameters, library
parameters, and templates of modes reached only through core code. The files go
to `$FP_HOME/targeted/<hash>/`, are linted before anything is built, get their own baseline (the
cache key includes their contents), and are listed in the record and in `evidence.md`; `fix` flies
the same set. Not with `--suite autotest`, not for PX4.

## 6. Add your own scenarios

The shipped scenarios (hover, circle, missions, GPS loss, RC loss, battery failsafe, wind, pilot
sticks, guided commands) cover basic behaviours. A regression in something you changed in your
fork is only found if a scenario flies it. A scenario is one YAML file:

```yaml
name: hover
steps:
  - takeoff: 10
  - hold: 20
  - mode: LAND
  - wait_disarm: 120
expect:
  hold_drift_max_m: {lt: 1.5}
```

Put your scenarios in `$FP_HOME/scenarios/` (they are added to the shipped ones; a name that is
also a shipped scenario is an error), or keep them anywhere and pass `--scenarios DIR [DIR ...]`
to `run`, `verdict`, `investigate` and `fix`. The step list, parameters, metrics and `expect:`
rules are described in [scenarios.md](scenarios.md). Check a file, then try it on a binary before
using it in an investigation:

```bash
forkpilot lint $FP_HOME/scenarios/                  # file and line of every mistake, before any flight
forkpilot run --firmware ~/fp/fork --binary $FP_HOME/builds/cache/<sha>/arducopter \
    --out /tmp/try -n 3 --only my_scenario
forkpilot verdict --candidate /tmp/try
```

`run` and `investigate` run the same check first and refuse to fly an invalid scenario.

Changing a scenario file makes ForkPilot fly the baseline for it again on the next investigation,
once; it is then cached.

`--suite autotest` flies ArduPilot's own Copter tests (about 400) from the same `good`/`bad`.
It needs the `autotest` extra. Use it as a second, wider screen; it confirms each drift on fresh
reruns before it reports it.

## Nightly

`forkpilot nightly` checks the commits that landed on an upstream branch since the last night,
the way a fork team would after a merge. Use a clone that only ForkPilot touches: it is fetched,
checked out and built in.

```bash
forkpilot nightly --repo ~/fp/upstream --branch master --vehicle copter plane
```

The first run checks the last 20 commits (`--first-range`); later runs check from the last
commit that finished. A range over `--max-commits` (default 60) is still investigated, and the
count is recorded. `--dry-run` fetches and prints the ranges without building or flying.

Records go to `$FP_HOME/nightly/`: `state.json` (last checked commit per branch, vehicle and
suite), `log.jsonl` (one line per vehicle and night) and `<date>/<vehicle>.json` with a
`summary.md`. A vehicle's state advances only if its investigation ended with an outcome; after
an `error` the same range is tried again the next night. Nothing is sent anywhere: the only
network use is the `git fetch`.

As a suggestion only (ForkPilot installs nothing), a crontab line:

```
30 1 * * *  FP_HOME=$HOME/fp forkpilot nightly --repo $HOME/fp/upstream --vehicle copter plane >> $HOME/fp/nightly.out 2>&1
```

## On-premises and LLM notes

- ForkPilot needs no network and no account for the pipeline above: detect, triage, bisect,
  evidence and fix-by-revert are all local and deterministic.
- A language model is optional and off unless you pass `--explain`, `--fix <backend>` or run
  `forkpilot explain`/`forkpilot fix --backend`. It never decides a verdict. It reads `evidence.md`
  (and, for fix proposals, the code at `bad`), a quote it makes that is not in the evidence is marked
  unverified, and every patch it proposes is built and flown by the oracle like any other candidate.
- For code that must not leave the site, use the `local` backend, any OpenAI-compatible server
  (vLLM, llama.cpp server, Ollama) inside your network:

  ```bash
  export FP_LLM_BASE_URL=http://llm.internal:8000/v1
  export FP_LLM_MODEL=<model name your server knows>
  export FP_LLM_API_KEY=...        # only if your server wants a bearer token
  forkpilot investigate --repo ~/fp/fork --good <sha> --bad <sha> --explain local --lang en
  ```

- The `anthropic` backend sends `evidence.md`, which contains the culprit commit's diff, to the
  Anthropic API. Do not use it on code you may not upload. There is no telemetry and no call home
  in ForkPilot itself.
- A container image for on-prem use is in the `Dockerfile` (Ubuntu 24.04, build prerequisites,
  ForkPilot). Mount your clone and a work directory. It was written against ArduPilot's prerequisites
  script and has not been built or run yet; treat it as a starting point and expect to fix it:

  ```bash
  docker build -t forkpilot .
  docker run --rm --user "$(id -u):$(id -g)" -v ~/fp/fork:/src -v ~/fp/work:/work \
      forkpilot doctor --repo /src --build
  ```

  `--suite autotest` on an older ArduPilot needs `unshare -rn` inside the container, which Docker's
  default seccomp profile blocks (`--security-opt seccomp=unconfined`).

## Simulation does not replace flight tests

ForkPilot flies a simulated multicopter (ArduPilot SITL, default quad model, one test field, ideal
sensors, simple wind). It finds changes in behaviour that the simulation can show and that a
scenario exercises. It does not see vibration, real sensor and ESC behaviour, radio and GPS in the
field, or anything your hardware does that SITL does not model. `no_regression` means "these
scenarios flew the same", not "the fork is safe". A `localized` culprit is a strong lead, not a
proof. Use ForkPilot to narrow down what to test and where to look, then fly it. Only Copter is
supported.

## When something goes wrong

| You see | Do |
|---|---|
| doctor: `FAIL empy` or `pexpect` | `pip install 'empy==3.3.4' pexpect` in the venv ForkPilot runs from. The waf build says `you need to install empy ...` when they are missing. |
| `build_failed`, or doctor `--build` fails | Read `builds/cache/<sha>/build.log`. A stale tree: `rm -rf <repo>/build` and retry. Missing submodules: `git submodule update --init --recursive`. |
| `<repo> is in use by another ForkPilot process` | One investigation per checkout at a time. Use a second clone for a second one. |
| `refusing to check out ... uncommitted changes` | Commit or discard changes to tracked files in the clone. |
| Disk below 5 GB | Free space or move `FP_HOME`. Old `investigations/<stamp>/` directories can be deleted; `results/baselines` and `builds/cache` are what saves time. |
| Many `infra:` errors, scenarios time out | Too many parallel SITLs for the machine: lower with `-j`. |
| SITL does not come up, ports 5760 and up in use | Another SITL or a second ForkPilot is running on the machine. Run one at a time. |

Environment variables: `FP_HOME` (work dir), `FP_BUILD_JOBS` (compiler processes), `FP_BUILD_NICE=1`
(build at low priority), `FP_NO_CCACHE=1`, `FP_LLM_BASE_URL`, `FP_LLM_MODEL`, `FP_LLM_API_KEY`,
`ANTHROPIC_API_KEY`.
