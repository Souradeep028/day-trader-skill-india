---
name: trader-exit
description: >-
  Exit Planner. Sets your stop and your target before you enter, so you know
  when to get out. Reads the entry from trader-entry (or a price you give),
  puts the stop at the opening-range midpoint by default (or the range low,
  your price, a percent, or a multiple of the ATR), rounds it down to the
  NSE tick, and sets the target at 2R (or 1R, or your price) on the tick. Refuses a stop at or
  above the entry, a target at or below it, or a stop too tight to survive the
  spread, and says why. Writes trader/plan.json for the Time Machine, the
  Budget Manager and Price Alerts. For a short (an entry.json with side sell, or --side sell) the stop goes above the entry, rounded up, and the target below. Use when the user says
  "where's my stop", "set my stop and target", "plan the exit", "when do I get
  out", "stop loss", "what's my R", "risk reward", or right after
  /trader-entry triggers, before /trader-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# trader-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap or a fast market can fill past it. Day trading loses most people money,
and a neat plan does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 2R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol INFY.NS --entry 1160.50 --or-high 1154.70 --or-low 1144.90
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--flat-by 15:10`, `--tick 0.05` (the stock's tick, if NSE set
it differently from its price band), `--json`, `--now "2026-11-05 09:40"`
(IST time), `--entry PRICE` with a waiting entry.json to plan ahead,
`--setup level`, `--offline FILE` (a saved Yahoo chart JSON for an ATR stop).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry: symbol, entry price, time, bar, opening range |
| `trader/config.json` | `flat_by` (15:10) and `min_reward_risk` (1.0: a note here, trader-risk enforces it after charges) |
| Yahoo chart API | only for an `atr:K` stop: the session's bars up to the entry bar, free and unofficial |
| `trader/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, tick, flat by. trader-timemachine, trader-budget and trader-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `or-high` | the opening-range high: a short's wide stop |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **The NSE tick.** Prices trade in steps that depend on the price (NSE
  circular CMTR/67133, in force since 15 April 2025): 0.01 under 250, 0.05
  from 250, 0.10 from 1,000, 0.50 from 5,000, 1.00 from 10,000 and 5.00 from
  20,000. An order off the tick is rejected, so the stop rounds DOWN to the
  tick (away from the entry, for a long) and the target rounds half up to it.
  NSE reviews each stock's tick monthly; `--tick` sets it by hand.
- **Risk** = entry minus stop. **Target** = entry + R x risk on the tick.
  `2R` by default; `1R`, `1.5R` or `price:X` also work. After charges a 1R
  target is usually a losing bet: trader-budget shows it.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price: too tight to survive the
  spread, normal wiggle would stop you out. An entry.json that has not
  triggered (unless you give `--entry`). For a short, the same with the sides
  swapped: the stop rounds UP, risk = stop minus entry, target = entry - R x risk.
- **Flat by.** 15:10 by default (or `flat_by` in the config). Out by then,
  win or lose, no overnight hold, and before Zerodha squares off open
  intraday (MIS) positions at about 3:20 PM for a fee. A flat-by at or after
  the 15:30 close is refused, and so is an entry at or after the flat-by time.

## Steps

1. Run it right after `/trader-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/trader-timemachine` to replay the plan, then `/trader-budget`
   to size it to the max loss per trade. Offer `/trader-alerts` for the stop,
   the target and the flat-by time. Claude can keep `entry.py watch` running
   in the background or on a `/loop` until a setup triggers, then come here.

## Example: the reel

INFY.NS, Thursday 5 November 2026. The entry is 1160.50 at 9:40 AM (the 9:35
bar, bar 4) and the opening range is 1144.90 to 1154.70. `exit.py plan --write`:

```
Exit plan: INFY.NS long, Thu 2026-11-05, opening-range breakout on 5-minute bars
  Entry         ₹1,160.50 at 9:40 AM (bar 4)
  Stop          ₹1,149.80  opening-range midpoint: (₹1,154.70 + ₹1,144.90) / 2 = ₹1149.8
  Target        ₹1,181.90  2R (two times the risk)
  Risk a share  ₹10.70 (0.92% of the entry)
  Reward:risk   2.0
  Flat by       15:10 IST, win or lose: no overnight hold
```

The midpoint is already on the 0.10 tick. A 1R target would be 1171.20.
trader-budget then sizes it: a 500 max loss with charges buys 41 shares.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It
  never prints a P&L or a return.
- Nothing here promises winning trades or money. A 2R target is not a
  prediction that the price gets there.
- A long by default; a short only when the entry is a sell (trader-entry
  `--side sell`) or `--side sell` is given.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, never puts a price off the NSE tick, and never calls a
stop a guarantee.

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
