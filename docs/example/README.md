# Example report

`report.html` is the report of one investigation from the synthetic benchmark
(`bench/make_fork.py`, case `ne_vel_assign`). An invented company, ACME, adds 31 commits to
ArduPilot master. One of them, presented as a pure refactor, changes `+=` to `=` in the
horizontal position controller. ForkPilot found it with 5 bisect tests.

Open the file in a browser. It has no scripts and makes no network requests.

Made with:

```bash
forkpilot investigate --repo ardupilot --good cafe674577 --bad bc2e397c5b
forkpilot --lang en report investigations/<stamp> --out docs/example/report.html
```

The good commit's baseline runs had been pruned, so the 5 `auto_mission` runs of `cafe674577`
were flown again from the cached build for the map (`forkpilot run --binary <cache>/arducopter
--only auto_mission -n 5`).
