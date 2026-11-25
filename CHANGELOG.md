# Changelog

Versions follow semver. A release is a git tag `v<version>`; the tag builds and publishes the Docker image
`ghcr.io/souradeep028/day-trader-india:<version>` (amd64 and arm64) once the tests pass. Pin a version for the
daily run and upgrade between forward tests, never during one: the code fingerprint shows a mid-test change.

## Unreleased

Fixes from a review of the research and the paper autopilot. A forward test running now sees a code change:
paper fills get worse by the test's own slippage, so start a new test (`forward.py start --replace`) rather than
mixing the two.

- **The live guardian** (`trader-broker/guardian.py`). Kite has no bracket order for stocks, so a live entry
  had no stop at the broker until someone placed it, and nothing cancelled the stop when the target filled. The
  guardian, armed by the user (`run --arm`, live accounts only), reads Kite every 5 seconds and may only protect or
  reduce: an SL-M at the approved stop for any uncovered shares, a moved-away stop restored, the pack's extra or
  orphaned legs cancelled, a market close past an approved stop, and everything closed at the flat-by. Every
  action is re-checked against a fresh read of Kite before it is sent; it never opens, adds, widens a stop or
  touches delivery. Dry run by default; `plan` works offline on saved Kite answers. Standard library only.
- **The daily loss limit counts what a trade really lost.** It added up today's entries' PLANNED max losses only,
  so a stop that slipped past its plan went uncounted: realised -1,132 on a 500 plan left room for another entry
  under a 1,500 limit. Each symbol now counts the larger of its planned max losses and its realised loss today
  after charges (plus the planned max loss of a position still open), with a warning naming the slip.
- **Live fails closed.** On a live account, a check that cannot run blocked nothing: no `trader/sectors.json`, no
  or a stale `trader/surveillance.json`, an unknown price band, no Nifty price, or no fresh price for a market order
  only printed a warning. Each now blocks a live entry (exits are never blocked); paper keeps the warnings.
- **One answer for the copied arithmetic.** `tests/test_parity.py` holds the five copies of Zerodha's charges, the
  four round trips, the two order fingerprints (trader-broker only shows an order trader-risk passed by its
  fingerprint) and the tick sizes and rounding to one result, so a copy cannot drift unnoticed.
- **Tests run clean under pytest too:** the repo-layout test ignores `.pytest_cache` and the like, and a helper
  pytest took for a test is renamed.
- **Paper fills like the shadow.** The autopilot charged no basis points of slippage: entries at Yahoo's last trade
  and stops at the stop, a tick worse. Every paper market fill (entry, stop, flat-by) now pays the frozen test's
  `slippage_bp` (3 by default) and a tick, exactly as the shadow replay does, so paper is never kinder than the
  shadow. On the frozen ORB rule 3 bp is about 0.075 R a trade, the size of the edge it is testing.
- **The autopilot's risk check sees the whole day.** It used to pass only the stock's price, so the 3 % Nifty
  halt and the NSE price band never fired on paper. It now passes Nifty's price and previous close (from the bars
  it already has) and the bands in `trader/bands.json` (`--bands`).
- **The gate counts days, not just trades.** The sequential gate's standard error is now the larger of the
  per-trade one and the one over whole days: a day's trades share the market's move. Simulated at a same-day
  correlation of 0.15 (the fixtures' figure), a rule with no edge passed 4.7 % of the time; now 2.5 %.
- **`trader/sectors.json` is in the repo**, with `scan.py sectors` to rebuild it from NSE's Nifty 500 list. It was
  ignored by `.gitignore`, so a fresh clone failed three tests and the Docker build, and the sector cap was off.
- **Honest wording.** Close fills are optimistic, not an upper bound. 5-minute research fills at the next bar's
  open are no delay at all, not "harsher than a real order". Paper's "Entry slippage" is drift, not a measured
  cost, and is no longer offered as the figure for backtests.
- **New tests:** no look-ahead (every signal is identical on the day cut right after it), paper and shadow exits
  are one rule, paper pays the shadow's slippage, the index halt and price band reach the autopilot, same-day
  trades count as one draw.
- **Burned sessions in research.py.** `trader-timemachine/burned.json` lists the sessions no test may count:
  13 August to 6 November 2026 (edge.py's grid picked the hypotheses there) and 7 November onward (the forward
  test's). `research.py run` leaves them out, refuses data that is all burned, and `--include-burned` explores
  them as IN-SAMPLE with nothing able to pass. A frozen plan records the list.
- **No verdict from the Monte Carlo.** timemachine.py no longer says "leans / strongly toward the target": its
  replays draw each bar on its own, so the odds reflect the stock's range and recent drift (a short gets the
  mirror image), not an edge. It prints the odds and a line saying what they are not.
- **One lock on the broker log.** ticket.py's writing commands (approve, decline, result, paper, paper-check,
  fill) and the autopilot's whole pass hold `trader/state/broker.lock`, so a record written by hand during a
  pass is no longer overwritten by the pass's last write.
- **A missing bar no longer drops a stock's day.** edge.py fills a bar Yahoo left out flat at the close before
  it (it adds no move of its own; a stop only the missing bar reached is missed) instead of leaving out the whole
  session, which chose sessions on what
  happened later. A session missing its 9:15 bar or more than 15 bars is still left out; the report says what
  it filled.

- **The gap-up test decides on what an order can see.** openclose.py's one passing rule (short a 2 % gap-up in
  the pre-open auction) picked its stocks on the official open, which is fixed at 9:08 after the last pre-open
  order. Its ten-year +0.28 R is now labelled an upper end. `trader-broker/preopen.py` saves a snapshot of the
  indicative prices taken from 9:00 to 9:08 (NSE's pre-open JSON saved from a browser, a Kite `get_quote` reply,
  or a CSV; it never fetches), and a gap test frozen now (`gapshadow.py start --replace`) decides on it and fills
  at the official open. A day without a snapshot is skipped with the reason; an older test warns in every status.
  The Docker scheduler alerts at 9:03 when today's snapshot is missing.
- **An off switch for the paper autopilot.** `"paper_autopilot": false` in trader/config.json makes every pass do
  nothing; the shadow replay after the close then judges the frozen rule, the machine need not be awake from 9:15
  to 15:30, and the pre-flight and the daily report know it is off.
- **The Time Machine's history keeps days with missing bars**, filled flat as edge.py does, instead of leaving them
  out (which picked days on what happened later). On the INFY fixture it adds 15 October: 21 trades, not 20,
  -0.37 R, not -0.35. The replays' pool still uses complete days only, since flat bars would understate the moves.

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

From the trading review (the edge has to survive honest fills and a test big enough to see it):

- **The sequential gate.** New forward tests are frozen with it: no PASS before 300 trades, then only when the
  average less 2.5 standard errors is above 0; STOP from 40 trades when clearly losing, from 300 while under
  +0.05 R, at 1,300 if still not proven, or on a 30 R fall. Simulated on these rules' spread, a rule with no
  edge passes about 3 % of the time (the old 40-trade gate: about 45 %). Old tests keep their 40-trade verdict,
  but trader-risk's live lock now needs a sequential PASS.
- **Realistic fills.** `edge.py --fills realistic`: in at the next bar's open, `--slippage-bp` on every market
  fill, targets only when traded through. The default (`close`) is unchanged and labelled an upper bound. The
  report shows the best rule with realistic fills at 0, 3 and 6 bp, and a rule only PASSES if it still makes
  money there at 3 bp. New shadow tests fill realistically (`forward.py start --fills realistic --slippage-bp`).
- **A baseline to beat.** Every stock on every session, the rule's side, stop width and target, in at 9:30
  whatever the chart did: a rule that does not beat it is the day's drift, not its setup.
- **Slippage in basis points.** The journal and the daily report give the autopilot's measured entry slippage in
  bp, the figure to feed back into `--slippage-bp`.
- **research.py**: about ten hypotheses written down in advance, tested walk-forward on years of bars you
  bring (a vendor's 1-minute CSVs, rolled up), with fills a minute after the signal, point-in-time index
  membership, a holdout recorded when it is looked at, and a test of picking the best rule so far before each
  fold. `trader.py research`.
- research.py **check** (unadjusted splits, bad prints, broken and short sessions, refused before a run),
  **power** (a planted edge in made-up bars: +0.056 R found 4 times in 10 with ~870 trades, 9 in 10 with ~3,400;
  no false passes), **plan** (hypotheses and settings frozen before the data), **bhavcopy** (NSE's daily files,
  adjusted with NSE's own previous closes, for openclose.py), the break-even slippage of each hypothesis, and a
  last-hour setup (`late`) for the research harness only.
- research.py **budget** (each hypothesis's trades projected to 50 stocks x 2 years against what a +0.06 R test
  needs; the untestable are dropped, the list now 10), **crosscheck** (a vendor's sample against the archive:
  catches bars labelled by their end, a one-minute look-ahead) and **plan --decision** (what you will do with the
  answer, written before it).
- edge.py's header now matches the grid: 96 rules, 16 filters, 1,536 combinations.

Upgrading from 2.x: pull, run `python3 core/trader.py migrate` in your trading folder, then `doctor`. If you
copied skill folders into `~/.claude/skills/`, copy `core/` there too. Wait for a running forward test to end
first.

## 2.0.0-india.1

The NSE day-trading pack: ten NSE skills and the Pelosi Tracker, paper autopilot, forward and shadow tests,
the daily report, the audit page and the Docker scheduler.
