---
name: trader-budget
description: >-
  Budget Manager. Sizes every position to your budget and your max loss per
  trade, charges included. Reads the trade plan from trader-exit (entry,
  stop, target), finds the whole shares that fit inside four limits (max loss
  per trade with Zerodha's intraday charges, position cap, max order, cash
  after your buffer), says which limit decided it, shows what a win at the
  target and a loss at the stop come to after charges, and writes ONE
  bracket order (an intraday MIS entry on the NSE, a stop-loss and a target)
  to trader/orders.json. A stop too far away for your max loss, or a target
  whose reward:risk after charges is under your minimum, is a plain refusal,
  not a smaller order. Nothing is placed. Use when the user says "how many
  shares", "size this trade", "how much should I buy", "I have 1 lakh", "I
  only want to risk 500 a trade", "what are the charges", "set my rules",
  "keep 10% in cash", or after /trader-exit writes a plan.
argument-hint: "[--budget RUPEES] [--limit [PRICE]]"
---

# trader-budget

The Budget Manager turns a plan ("buy INFY at 1160.50, stop 1149.80") into an
exact order ("buy 41 shares"), inside rules the user sets once. The stop
decides the size: the further away the stop, the fewer shares. Charges count
against the max loss, so a small account loses more of each trade to them.
It writes a proposal only. trader-risk checks it and trader-broker asks the
user yes or no. Day trading loses most people money. Sizing to a max loss
keeps each loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write         # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1160.00 --json  # a limit order at 1160.00
python3 "${CLAUDE_SKILL_DIR}/size.py" --targets "${CLAUDE_SKILL_DIR}/targets.example.json" --budget 100000   # weights mode
```

`--now "2026-11-05 09:40"` pins the clock (IST). A plan for another day is
refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules and the charges (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time (trader-exit writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (trader-alerts writes it) |
| `trader/portfolio.json` | optional: its cash caps the size (trader-broker keeps it) |
| `trader/orders.json` | the one proposed bracket order it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words:

- **budget**: how much money this is for, in rupees (example 1,00,000). Only
  money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills
  at the stop, charges included (default 500, half a percent of 1 lakh).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest
  position (75% of the budget), the biggest order (75,000) and the smallest
  order (500).
- The day limits trader-risk enforces: **max_daily_loss_inr** (1,500),
  **max_trades_per_day** (3), **min_reward_risk** (1.0, after charges),
  **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "14:45") and **flat_by** ("15:10"), all IST, plus
  **max_losing_streak** (2: no new entries after two losing trades in a row
  today), **max_entries_per_sector** (2 a day in one industry),
  **max_weekly_loss_inr** (3,000) and **max_monthly_loss_inr** (6,000),
  **max_same_side_risk_inr** (1,000 at risk in open longs, or open shorts),
  **max_index_move_pct** (3: no entries on a 3 % Nifty day),
  **skip_ex_dates** and **skip_market_event_days** (true),
  **max_charges_pct_of_risk** (15: the round trip's charges at the stop at
  most 15 % of the loss on price), **skip_results_days** (true),
  **skip_expiry_days** (false: a warning on expiry days) and
  **block_surveillance** (true: no entries in stocks on NSE's ASM or GSM
  lists). 15:10 is before
  Zerodha squares off open intraday positions itself (about 3:20 PM, for a
  fee).
- **charges**: Zerodha's equity intraday rates are the default (read from
  zerodha.com/charges on 8 November 2026). Another broker's rates go here;
  check them on its own charges page.
- **intraday_leverage** stays 1 (no borrowing) unless they understand MIS
  margin and also set **allow_margin** true. The most is 5.
- **blocked**: symbols they never want to trade.
- Keep **paper** true until they have watched it work for a while.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only: NSE equity trades in lots of one. The size is the
smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | the most shares whose loss at the stop plus the round trip's charges fits `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) x `intraday_leverage` / entry, never more than the cash in `trader/portfolio.json` allows |

The charges on one intraday order: brokerage 0.03% or 20 rupees, whichever is
lower; STT 0.025% on the sell; NSE exchange charges 0.00307%; SEBI 10 rupees
a crore; stamp duty 0.003% on the buy; GST 18% on brokerage, exchange and
SEBI charges. Each is worked to the paisa; the broker's contract note is the
final word.

The entry it sizes at: the limit price with `--limit`. For a market order, the
plan's entry, or the price in `trader/prices.json` if it is fresh (under 15
minutes old) and higher. A price that ran up since the plan means a bigger
loss at the same stop, so fewer shares. A lower price never shrinks the risk.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk after charges is under `min_reward_risk`, the
charges at the stop are over `max_charges_pct_of_risk` of the risk (a stop so
close that charges eat the trade: it says how far a stop needs to be), the
stop is not below the entry, the target is not above it (the move already
happened), the plan is for another day, the symbol is blocked, leverage is
set without `allow_margin`, or the plan is a short while `allow_short` is
false (or not intraday MIS). Say the reason to the
user in one line.

Why the charges limit: charges are mostly a percentage of the money traded,
so the closer the stop, the more shares, and the bigger the charges against
the same max loss. trader-timemachine's edge report found charges at the
stop were a median 16.8 % of the risk across the Nifty 50, and the trades
over 15 % were where most of the losses after charges came from. The size
report shows the share on its "At the stop" line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees,
the loss at the stop with charges and the win at the target after charges.
Say plainly that a stop is an order, not a promise: in a fast market it can
fill below the stop. Then hand over to `/trader-risk`.

## Example: the reel

INFY on Thursday 5 November 2026. Opening-range breakout: entry 1160.50 at
9:40 AM, stop 1149.80 (the opening-range midpoint), target 1181.90 (2R). Risk
10.70 a share. The example config: 1 lakh budget, 10% cash buffer, 500 max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | 41 x 10.70 = ₹438.70 + ₹50.30 charges = ₹489 (42 would be ₹500.91) | **41 (decides it)** |
| position cap | 75% of ₹1,00,000 = ₹75,000 / 1160.50 | 64 |
| max order | ₹75,000 / 1160.50 | 64 |
| cash | ₹1,00,000 minus ₹10,000 = ₹90,000 / 1160.50 | 77 |

41 shares = ₹47,580.50. At the stop: lose ₹438.70 plus ₹50.30 charges =
₹489. At the target: win ₹877.40 minus ₹51.13 charges = ₹826.27.
Reward:risk after charges 1.69. It writes:

```json
{"symbol": "INFY.NS", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "41", "type": "market",
 "time_in_force": "day", "order_class": "bracket", "take_profit_limit_price": "1181.90",
 "stop_loss_stop_price": "1149.80",
 "why": "opening-range breakout on INFY.NS: 41 shares, bound by your ₹500 max loss per trade (41 x 10.70 = ₹438.70 at the stop + ₹50.30 charges = ₹489)",
 "plan": {"entry": 1160.5, "stop": 1149.8, "target": 1181.9, "risk_per_share": 10.7, "max_loss": 489.0,
          "charges_at_stop": 50.3, "charges_at_target": 51.13, "gain_at_target": 826.27,
          "setup": "orb", "entry_time": "9:40 AM", "flat_by": "15:10"}}
```

`why` and `plan` are notes for trader-risk and trader-broker; they are never
sent to a broker. With a 1R target (1171.20) instead, the same 41 shares win
₹387.85 after charges and lose ₹489: reward:risk after charges 0.79, so it is
refused, though it looks like 1.00 on price alone.

## Weights mode (kept from v1)

`--weights RELIANCE.NS=40,TCS.NS=30,INFY.NS=30` or `--targets FILE`
(`targets.example.json` here) splits a budget across a list by weight: the
cash buffer comes off the top, weights are renormalised to 100, any stock
over `max_position_pct` is pinned at the cap and its excess shared out,
slices under the minimum order are dropped, and amounts round to whole rupees
so nothing is left idle by accident. With `"fractional": false` (the example
config) it buys whole shares and needs prices (`--prices trader/prices.json`).
These are delivery (CNC) buys with no stop: trader-risk blocks them while
`require_stop` is true. Use them for a long-term split, not a day trade.

## Shorts (off by default)

An intraday (MIS) short sells first and buys back by the flat-by. It is off
until `trader/config.json` says `"allow_short": true`; the edge report's
best lead so far is a short, which is why it exists. With it on:
`entry.py --side sell` watches for a close below the range low,
`exit.py` puts the stop above the entry and the target below,
`size.py` sizes it (STT on the sale, stamp duty on the buy back),
`check.py` blocks a CNC short and a sale near the lower price band,
`ticket.py` sends SELL then a BUY SL-M stop and a BUY LIMIT target (or
fills them on paper), and `alerts.py` rings when the price rises to the
stop. A short's loss has no ceiling if the stock gaps up through the stop,
and a stock locked at its upper band cannot be bought back until it
reopens: paper trade shorts first.

## What it never does

It never places an order, never sizes a short unless `allow_short` is on, never rounds up past a limit,
never hides charges, and never treats a refusal as "buy fewer then". A
refusal means skip the trade or make a new plan.

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
