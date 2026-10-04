# Security

ForkPilot builds and runs the firmware that you point it at, on your own machine. Use it only
on code that you trust.

## Report a vulnerability

Do not open a public issue. Use GitHub's private vulnerability reporting: the **Security**
tab of this repository, then **Report a vulnerability**. You get an answer within 7 days.

## Scope

- In scope: ForkPilot's own code, for example a scenario or log file that makes ForkPilot
  run commands or write outside its work directory, or a report that leaks data.
- Out of scope: bugs in ArduPilot or PX4. Report them to those projects.

## LLM backends

`explain` and `fix --backend` send `evidence.md` of an investigation (oracle findings,
telemetry summary, culprit diff) to the configured model. `fix` also sends the culprit's
source files as they are at `bad`. With `--backend anthropic`, this
data leaves your network. With `--backend local`, it goes only to the OpenAI-compatible
endpoint that you set in `FP_LLM_BASE_URL`.
