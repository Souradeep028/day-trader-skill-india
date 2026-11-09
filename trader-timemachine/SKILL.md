---
name: trader-timemachine
description: >-
  Time Machine. Replays every possible outcome of a trade before you risk a
  rupee: 10,000 simulated sessions plus every past time the setup fired. Takes
  the plan from trader-exit (entry, stop, target), rebuilds the rest of the day
  10,000 times from the stock's own 5-minute bars over the last 60 days, and
  re-runs the same rule on every past session. Prints the odds of target first,
  stop first, or neither by your flat-by time, where the price sits then, the
  average R before and after Zerodha's charges, the history, and one honest
  verdict, then hands off to /trader-budget. Its edge report (edge.py) tests
  1,536 intraday rules across a list of stocks (opening-range breaks,
  previous-day breaks, VWAP pullbacks; long and short; with filters for Nifty,
  volume, gaps, VIX, charges and expiry) after charges and slippage, in two
  halves, with a bootstrap luck test. Its swing report (swing.py) tests 40 multi-day delivery rules on ten
  years of daily bars against holding the same stocks and Nifty. Use when the user says "run the time
  machine", "replay this trade", "what are the odds", "simulate this trade",
  "monte carlo", "backtest this setup", "how often does this hit the target",
  "is this a coin flip", "does any setup make money", "run the edge report", or after /trader-exit writes a plan, before
  /trader-budget.
argument-hint: "[path to plan.json, or SYMBOL.NS]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
days, how often does this plan hit the target first, the stop first, or
neither? It answers with 10,000 replays and with the real record of the same
rule on every past session. It is a simulation built from the stock's own
recent bars, not a prediction. Day trading loses most people money, and a
good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:00      # end each replay earlier than the flat-by
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol INFY.NS --date 2026-11-05 \
    --entry 1160.50 --stop 1149.80 --target 1181.90 --entry-time "9:40 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--qty N` (the shares for the charges), `--slippage-ticks N`
(default 1, 0 for none), `--config FILE`
(`max_loss_per_trade_inr`, `charges` and `flat_by`, default
`trader/config.json`), `--offline FILE` (a saved Yahoo chart JSON, `.json`
or `.json.gz`), `--now "2026-11-05 09:45"` (IST) to pin the clock.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by |
| `trader/config.json` | the max loss that sizes the charges, the charge rates and the default flat-by |
| Yahoo chart API | one request: 60 days of 5-minute bars (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths, a 24-bin histogram of the close and the charges |

## How it works

1. **The pool.** It keeps the sessions before the plan's date that have every
   five-minute bar from the 9:15 open to the replay's end (the plan's
   flat-by, 15:10 by default). Yahoo's NSE feed usually sends the 15:15 to
   15:25 bars empty, so a replay to the 15:30 close finds no full sessions;
   a day trade out by 15:10 does not need them. Days with gaps, and today,
   are left out. Every bar becomes four ratios to the bar before it: open,
   high, low and close over the previous close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default: the plan's
   flat-by). Green, which means target first or above the entry at `--until`.
   R is the target's R at the target, -1 at the stop, and otherwise the
   distance from the entry at `--until` divided by the risk. A gap through
   the stop still counts as -1R here. A real fill can be worse.
4. **Charges.** The average R is shown again after charges: Zerodha's equity
   intraday rates for the shares trader-budget would buy (the most whose loss
   at the stop plus charges fits the max loss). On a small trade the round
   trip's charges are a real slice of 1R. The same line takes off slippage:
   one tick (`--slippage-ticks`) on the market entry, and on the exit when it
   is a stop or the flat-by sale (a target is a limit order and fills at its
   price), on the tick the stock traded at that day. When charges turn a
   small average gain into a loss, it says so.
5. **The history.** The plan's own rule (the same opening range, stop rule
   and target R, stops and targets on the NSE tick) runs again on every one
   of those past sessions. That is what really happened, not a simulation.
6. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them.
7. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (wrong date, not closed yet, a future day), or when the plan
does not make sense (a stop above a buy's entry, an entry off the 5-minute
grid). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. The replays end at the plan's flat-by,
   so "neither" means still open at the time you would get out.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the flat-by, the average R before and after charges,
   the history line, then the verdict line and any line under it exactly as
   printed. Say the seed.
4. If the verdict is a coin flip, close to break-even or toward the stop, say
   so plainly. Do not dress it up. If the history and the replays disagree,
   say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Results, news and halts can do things those 60 days never did. Check
   `/trader-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example: the reel

INFY.NS, Thursday 5 November 2026. The 15-minute opening range is 1144.90 to
1154.70. The 9:35 bar closes above it: entry 1160.50 at 9:40 AM (bar 4), stop
1149.80 (the range midpoint), target 1181.90 (2R, risk 10.70 a share).

```
10,000 replays of the rest of the session, 9:40 AM to 3:10 PM (66 five-minute bars), seed 19.
  target first   4.9 %
  stop first     53.5 %
  neither        41.6 %   still open at 3:10 PM
  green          31.2 %   target first, or above the entry at 3:10 PM
  Price at 3:10 PM: median 1155.92, 90 % of replays between 1135.79 and 1177.07.
  Average R: -0.316 (R = the 10.70 risk: +2R at the target, -1R at the stop).
  After charges and slippage: -0.450 R. A round trip of 41 shares costs about ₹50.57, 0.115 R;
  1 tick of slippage (0.10) on the entry and on a stop or flat-by exit, 0.018 R.
History: the same rule (15-min opening range, stop at the opening-range midpoint, 2R target) on each
of those 57 real sessions fired 20 times: 1 target, 10 stop, 9 neither. Average R -0.349, -0.482 after
charges and slippage.
Strongly toward the stop: at 2R the target has to come first in 33 % of the replays that hit one side;
it did in 8 % (target first 4.9 % vs stop first 53.5 %).
41.6 % of the replays hit neither by 3:10 PM; the price at 3:10 PM decides those.
```

That is the honest answer for this setup on INFY in those 57 sessions, and
it is what the user should hear before `/trader-budget` sizes 41 shares. On
the day itself the trade hit neither side and closed at 15:10 for a small
gain after charges: one day says little either way.

## The edge report: does any setup make money after charges?

One plan's replay says little about a setup. `edge.py` in this folder runs a
whole grid over a list of stocks and every session in the data:

```bash
python3 "${CLAUDE_SKILL_DIR}/edge.py" run --watchlist trader/nifty50.txt          # Yahoo, 60 days of 5-min bars
python3 "${CLAUDE_SKILL_DIR}/edge.py" run --watchlist trader/nifty50.txt --write  # also trader/edge.json
python3 "${CLAUDE_SKILL_DIR}/edge.py" run --watchlist trader/nifty50.txt --setups orb30 --sides short
python3 "${CLAUDE_SKILL_DIR}/edge.py" run --symbols INFY.NS,TCS.NS --slippage-ticks 2 --max-cost-pct 10
```

- **96 rules**: six setups, long and short, a tight or a wide stop, a 1R,
  1.5R, 2R or 3R target, entries 9:30 to 14:45, out by 15:10.
  - `orb15`, `orb30`: the first close beyond the 15- or 30-minute opening
    range. Stops: the range midpoint, or the far side of the range.
  - `orb15s`, `orb30s`: the same ranges with a stop entry: a buy-stop a tick
    over the range high (a sell-stop under the low) that fills the moment the
    price gets there, or at the open of a bar that gaps through. The filters
    read the bar before the fill, so nothing is known before it happened.
  - `pdb`: the first close beyond the previous session's high (low for a
    short), having closed on the other side the bar before.
  - `vwap`: a pullback to VWAP that closes back on the trend's side, with the
    price above the day's open (below, for a short).
  - For `pdb` and `vwap` the tight stop is the entry bar's low (high), the
    wide stop the day's low (high) so far.
- **16 filters**: none; `with_nifty` (Nifty has moved the trade's way since
  9:15); `stronger` (the stock has moved its way more than Nifty);
  `volume` (the entry bar beat the day's average bar so far); `no_gap`
  (opened within 1 % of the last close); `calm_vix` (India VIX opened under
  its 20-session median); `low_cost` (the charges at the stop are at most
  15 % of the risk, `--max-cost-pct`, the same rule as trader-budget's
  `max_charges_pct_of_risk`); `not_expiry` (not Nifty's weekly expiry);
  `not_10_to_11` (no entries 10:00 to 11:00, the worst hour for the default);
  and `low_cost` together with each of the others. 1,536 combinations.
- **Each trade the way the pack would take it**: sized like trader-budget
  (the most shares whose loss at the stop plus charges fits the max loss),
  Zerodha's charges (a short pays STT on its sale and stamp duty on its buy),
  on the tick the stock really traded at that day, one tick of slippage on
  the entry, the stop and the flat-by exit (a stop that gaps fills at the
  bar's open). R is the result after charges over the planned risk.
- **Two halves and a luck test**: a rule "held up" when it made money after
  charges in both the first two thirds of the sessions and the last third,
  with 30 or more trades in each. With 1,536 combinations tested, some will
  hold up by luck, so a rule only PASSES when a session bootstrap (whole
  days resampled, since one day's trades move together) also gives it a
  p-value under 0.05 / 1,536 = 0.000033.

- **What one person would have made**: the grid takes every signal on
  every stock; a person takes 3 a day. The report scales each rule to that
  (`--trades-per-day`), takes 30 % tax off a profit (`--tax-pct`, the slab
  rate on speculative income) and sets it against a `--capital` account and
  against simply holding Nifty 50 over the same sessions.

**What it found** on the Nifty 50 over the 58 sessions to 6 November 2026:

- **Nothing passed.** 34 of 1,536 combinations held up in both halves, the
  best with a p-value of 0.13: well inside what luck produces.
- **Charges are the problem.** At the stop they were a median 16.8 % of the
  risk, so most trades started 0.15 to 0.2 R behind. The pack's default
  (long 15-minute range break, midpoint stop, 2R) took 1,607 trades and lost
  ₹1,15,780, ₹1,06,901 of it charges. For one person taking 3 a day on
  ₹1,00,000 that is -12.5 % in 58 sessions, while holding Nifty 50 made
  +2.5 %. Keeping only trades whose charges were under 15 % of the risk took
  the average rule from -0.28 R to -0.09 R: why trader-budget and
  trader-risk refuse the rest.
- **Stop entries get a better price and lose more.** Unfiltered, the long
  15-minute range break went from -0.26 R on a close entry to -0.38 R on a
  buy-stop: the stop also catches every wick that pokes through the range and
  falls back, which waiting for the close filters out.
- **Skipping 10:00 to 11:00** barely moves the average rule (-0.25 R to
  -0.23 R). It lifts the short 30-minute lead to +0.11 R and +0.10 R in the
  two halves (p 0.13), but that hour was picked from this same data.
- **The best lead** is still a short on the 30-minute range, low cost, with
  Nifty falling or outside 10 to 11: about +0.1 R a trade after charges,
  roughly 5 % on ₹1 lakh after tax over the 58 sessions in hindsight, and
  within luck. Shorts are off by default (`allow_short`). Freeze it with
  trader-broker's `forward.py` and paper trade it before any money goes in.

Say this plainly when the user asks whether a setup works. Run the report
again on fresh data before trusting any rule, and paper trade a lead before
any money goes in.

## The swing report: multi-day trades on ten years of data

Intraday rules lose to charges because the moves are small next to them.
`swing.py` in this folder tests trades held for days, in delivery (CNC),
where Zerodha charges no brokerage, on ten years of free daily bars:

```bash
python3 "${CLAUDE_SKILL_DIR}/swing.py" run --watchlist trader/nifty50.txt          # Yahoo, 10 years of daily bars
python3 "${CLAUDE_SKILL_DIR}/swing.py" run --watchlist trader/nifty50.txt --write  # also trader/swing.json
```

- **40 rules**: entries `breakout20` and `breakout55` (a close over the 20
  or 55 closes before), `pullback` (over the 200-day average with a 2-day RSI
  under 10) and `oversold` (a bounce after a 14-day RSI under 30), each
  bought at the NEXT day's open; exits after 5, 10 or 20 days, a trailing
  exit under the 10-day low, or a close over the 5-day average; every trade
  with a 2 x ATR(14) stop; with and without Nifty over its 200-day average.
- **Sized and charged for real**: ₹1,000 at risk a trade (`--risk`), at most
  `--capital` / `--positions` (₹20,000) a position, delivery charges (STT
  0.1 % each side, stamp duty 0.015 % on the buy, a ₹15.34 DP charge on each
  sale).
- **Four tests to pass**: money in both the earlier and the later years, in
  at least 60 % of the years, more than luck explains across 40 rules (a
  bootstrap over months), and more than the **drift**: simply buying the
  same stocks on any day and holding as long. Today's Nifty 50 are the
  stocks that survived ten years, so every long rule on them looks good; the
  drift carries the same bias, and beating it is what counts.
- It also says what one person would have made a year with 5 positions on
  ₹1,00,000 after 20 % short-term capital gains tax, against holding Nifty.

**What it found** on the Nifty 50, September 2017 to November 2026: nothing
passed. Most rules made money only as much as the drift did. One beat the
drift and made money in both halves and 6 of 10 years (a pullback in an
uptrend, trailing exit, Nifty over its 200-day average: +0.13 R a trade),
but at p 0.03 against a bar of 0.00125 that is within luck. The best rule
by average R made about 7.6 % a year after tax with 5 positions; holding
Nifty 50 made 10.6 % a year. Say so plainly: on this evidence an index fund
beat every rule tested, intraday or swing.

## The factor rotation report: momentum and low volatility

`rotation.py` tests the two factors with the most evidence behind them,
rebalanced once a month on the Nifty 100 (`trader/nifty100.txt`) over ten
years of daily bars:

```bash
python3 "${CLAUDE_SKILL_DIR}/rotation.py" run --watchlist trader/nifty100.txt
python3 "${CLAUDE_SKILL_DIR}/rotation.py" run --watchlist trader/nifty100.txt --capital 500000 --write
```

- **12 portfolios**: momentum (the return from 12 or 6 months ago to a
  month ago) or low volatility (the steadiest 252 days), the top 10 or 20,
  with and without a trend filter (cash while Nifty closes the month under
  its 200-day average). Ranked on each month's last close, traded at the
  next open, whole shares, delivery charges, tax paid each April (20 % short
  term, 12.5 % long term over ₹1.25 lakh, today's rates throughout).
- **Benchmarks**: the same stocks held equal-weight (it carries the same
  survivorship bias, so beating it is what counts) and Nifty 50 held.
- **The reality check**: real factor ETFs (Aditya Birla Sun Life's Nifty
  200 Momentum 30, `MOMENTUM.NS`; ICICI Prudential's Nifty 100 Low
  Volatility 30, `LOWVOLIETF.NS`) against the Nifty 50 ETF `NIFTYBEES.NS`
  over the same dates. Their indexes picked the stocks at the time, so they
  have no survivorship. When the backtest and the funds disagree, believe
  the funds.

**What it found**, December 2017 to November 2026: the backtest looks
spectacular and is not to be believed. Today's Nifty 100 held equal-weight
made +21.7 % a year, against +10.3 % for the Nifty 50 itself: that gap is
survivorship, the companies that grew into the index. Momentum top 10 made
+32.5 % a year after tax, but against the equal-weight list it was within
luck (p 0.047, the bar 0.0042) and lagged it in the last three years;
low volatility trailed it every way. Nothing passed. The real funds tell the
truth: the momentum ETF made 8.9 % a year from August 2022 against 9.5 % for
NIFTYBEES (and fell 31 % against 15 %); the low-volatility ETF made 11.9 % a
year from May 2018 against 11.6 %, with a smaller worst fall (28 % against
36 %). Low volatility matched the index with a smoother ride; momentum did
not beat it in the years a real fund has existed.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`): ticket.py fills paper orders itself and no broker sees
  them. Going live is the user's own decision, in their own config.
- Nothing in this pack places a real order except trader-broker, and only when all three hold:
  trader-risk passed that exact order today, the user typed an explicit yes for that one order in the
  chat, and the user clicked Allow once on Claude's own card because the order tool is set to "Needs
  approval" (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No futures and options, margin (MIS leverage) or short selling unless the config allows it and the
  user confirms.
- Broker keys (the Kite Connect API key and secret) live in the Claude Desktop config, never in the chat.
