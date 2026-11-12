# Changelog

Versions follow semver. A release is a git tag `v<version>`; the tag builds and publishes the Docker image
`ghcr.io/souradeep028/day-trader-india:<version>` (amd64 and arm64) once the tests pass. Pin a version for the
daily run and upgrade between forward tests, never during one: the code fingerprint shows a mid-test change.

## 3.0.0

The platform release: the pack as something anyone with Claude Code can install, upgrade and trust.

- **One copy of the shared tables.** The NSE holidays, special sessions, tick sizes and Zerodha's charges moved
  from eleven copies in the skill scripts to `core/nse.json` and `core/brokers/zerodha.json`, read through
  `core/tradercore.py`. A new year's holidays is one edit. The code fingerprint now covers `core/`.
- **One command.** `core/trader.py` runs every tool (`trader.py check`, `trader.py daily`, ...) plus the pack's own
  commands: `setup`, `doctor`, `migrate`, `stop`, `resume`, `version`, `mcp`, `package`. In Docker: `trader`.
- **`doctor`.** A health check of the install and the trading folder (OK / WARN / FAIL), `--online` for Yahoo
  and the clock, `--bundle` for a redacted support file.
- **Config versions.** `trader/config.json` carries `config_version` (now 3). `trader.py migrate` brings an older
  one up (adds `"broker": "zerodha"`), keeps a backup and never changes a limit. Unknown keys are flagged.
- **The stop switch.** `trader/STOP` blocks every new entry, paper or live, and makes the pre-flight NO-GO.
  Exits are never blocked. Only `trader.py resume` turns it off.
- **An MCP server in the plugin.** `core/mcp_server.py`, standard library: `doctor`, `status`, `forward_tests`,
  `journal`, `daily_report`, `schedule` (read only) and `stop`. No order tool, by design.
- **Releases.** CI on Python 3.8 to 3.13 and macOS; a tag-triggered release that publishes a multi-arch image;
  `docker/compose.release.yaml` to run a pinned version without building.
- **Standalone skill zips.** `trader.py package` builds a skill plus the shared tables, for claude.ai.
- **The Pelosi Tracker is its own plugin**, `congress-trades`, in the same marketplace: it is US-only.
- **Fine print.** The README says plainly what the pack is (an education and research tool, run by each user for
  their own account) and is not (advice, signals, or a service for others).

Upgrading from 2.x: pull, run `python3 core/trader.py migrate` in your trading folder, then `doctor`. If you
copied skill folders into `~/.claude/skills/`, copy `core/` there too. Wait for a running forward test to end
first.

## 2.0.0-india.1

The NSE day-trading pack: ten NSE skills and the Pelosi Tracker, paper autopilot, forward and shadow tests,
the daily report, the audit page and the Docker scheduler.
