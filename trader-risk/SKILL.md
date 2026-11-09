---
name: trader-risk
description: >-
  Risk Manager. Checks every trade against your limits and stops anything
  over them, before it can reach the broker. For an NSE day trade: a stop is
  required, the loss at the stop plus the round trip's charges must fit your
  max loss per trade (measured from the higher of the plan's entry and a
  fresh price), reward:risk after charges must meet your minimum, the day's
  total risk must fit your daily loss limit, no more than your trades per
  day, no new entries outside your entry window or when the NSE is closed,
  and no buy right under the stock's upper price band (circuit limit). It
  also keeps the basics: NSE symbols only, no futures and options, whole
  shares, MIS or CNC, blocked symbols, order size, position cap, cash buffer,
  intraday leverage only when you allow margin, short sales, malformed or bad
  bracket orders, duplicates and a daily order limit. Getting out is never
  blocked by a day limit. Prints PASS or BLOCK with the reason and records
  the verdicts so trader-broker only shows an order that passed. Use when the
  user says "check my order", "is this safe", "risk check", "does this break
  my rules", "can I take this trade", "how much have I risked today", or
  before /trader-broker, always.
argument-hint: "[path to orders.json]"
---

# trader-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by trader-budget or trader-broker's flatten passes through it. A
BLOCK is final for that order: it does not go to the user for a yes, and it
does not go to the broker. SEBI's own study of Indian intraday traders found
about seven in ten lost money. These limits keep each loss and each day's
losses small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-11-05 09:40"  # pin the clock (IST)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget, or trader-broker's flatten) |
| `trader/config.json` | the limits and the charges (trader-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry it measures risk from; its previous close feeds the band check |
| `trader/bands.json` | each stock's NSE price band (trader-scanner: `scan.py universe`) |
| `trader/sectors.json` | each Nifty 500 stock's NSE industry, for the sector cap (copy it from this pack's `trader/` folder) |
| `trader/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset and short sales
only. Every other rule (day limits, entry window, loss, reward:risk, band,
blocked list, size, duplicates) turns into a warning for an exit, never a
BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol; side not buy or sell; no whole-share qty (the NSE takes no rupee-amount orders); exchange not NSE; product not MIS or CNC; a limit order with no limit price; validity not day or ioc; extended hours (the NSE has none); a bracket that is not day validity or misses its stop-loss or target |
| asset | futures and options (like `INFY26NOVFUT`, `NIFTY26N1225000PE`) while `allow_options` is false; anything that is not an NSE symbol like `INFY.NS` |
| short | a sell of more than you hold (`allow_short` false) |
| blocked | (entries) the symbol is on the `blocked` list |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the cash it needs (the value, divided by `intraday_leverage` when `allow_margin` is true) would leave less than the cash buffer, or more than you have |
| stop | (entries) no stop while `require_stop` is true (a bracket `stop_loss_stop_price`, or the order's plan stop); a stop on the wrong side of the entry |
| loss | (entries) qty x (entry minus stop) plus the round trip's charges over `max_loss_per_trade_inr` |
| reward:risk | (entries) the win at the target after charges / the loss at the stop with charges, under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` (09:30) or after `no_entries_after` (14:45), or the NSE is closed |
| band | (entries) the entry is within 1% of the upper price band: the previous close plus the stock's band (2, 5, 10 or 20%) from `trader/bands.json` |
| cool-off | (entries) `max_losing_streak` (2) losing trades in a row today, after charges: no new entries for the rest of the day |
| sector | (entries) `max_entries_per_sector` (2) entries in one NSE industry today, this batch included (`trader/sectors.json`) |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a limit order. For a
  market order, the HIGHER of the plan's entry and a fresh price from
  `trader/prices.json` (under 15 minutes old). A plan written at 9:40 cannot
  understate the risk at 9:50 after the price ran up. An older price is not
  used, and it says so.
- **Charges** are Zerodha's equity intraday rates on the NSE (brokerage 0.03%
  or 20 rupees an order, STT 0.025% on the sell, exchange 0.00307%, SEBI 10
  rupees a crore, stamp 0.003% on the buy, 18% GST on brokerage, exchange
  and SEBI), the same in every skill. Another broker's go in `"charges"` in
  the config. On a 1R target they can turn an even bet into a losing one.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as trader-broker logged it. Three full-size trades use up a ₹1,500 day.
- **The window** is IST, worked out in the tool (UTC+5:30, no daylight
  saving), never from the computer's clock zone. Weekends and NSE holidays
  (the 2026 list) are closed, and so is anything outside 9:15 to 15:30. Set
  both `no_entries_*` to null to switch the window off.
- **Price bands**: a stock at its band can lock, with no sellers above it (or
  buyers below it). F&O stocks have no fixed band (null in bands.json) and
  are not checked; a stock missing from bands.json is a warning. A stop under
  the lower band is a warning: if it locks there, the stop cannot fill.
- **Cool-off and sector cap**: two losses in a row is the moment people
  trade to win it back; the cool-off stops new entries for the day. Stocks
  in one industry move together (one IT selloff can stop out three IT
  trades at once), so the sector cap keeps the day's risk spread. A
  breakeven trade on price still lost its charges, so it counts as a loss.
- **Leverage**: MIS positions can borrow up to about five times your cash
  under SEBI's margin rules. Here `intraday_leverage` is 1 (no borrowing)
  unless the config also says `"allow_margin": true`, and never more than 5.
  The max loss is on the full position either way.

Warnings do not block, but say them to the user: a LIVE account, a number
sent as a number instead of a string, a stop that lives only in the plan and
not at the broker, a price too old to use, a delivery (CNC) product, borrowed
money, a holiday list that does not cover the date.

## Steps

1. If the price may have moved since the plan, refresh it first
   (trader-alerts: `alerts.py quote INFY.NS --write`). Run `scan.py universe`
   in the morning so `trader/bands.json` is today's.
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with charges, reward:risk after charges, and how much of today's
   limits it uses. For a BLOCK, give the reason and the fix (fewer shares, a
   closer stop from trader-exit, wait for the window, or skip the trade).
   Never change a limit yourself.
4. If the order changes in any way, run the check again. trader-broker matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: the reel

The INFY bracket from trader-budget at 9:40 AM on Thursday 5 November 2026,
on a fresh 1 lakh paper account with the example config:

```
PASS  #1 buy INFY.NS 41 shares, entry
          entry 1160.50 (the plan's entry), stop 1149.80, target 1181.90: risk 10.70 a share
          max loss ₹489 with ₹50.30 charges (limit ₹500), reward:risk after charges 1.69 (min 1)
          today before this: 0 of 3 trades, ₹0 of ₹1,500 daily loss

1 passed, 0 blocked at 2026-11-05 09:40 IST. Cash after the passed orders: ₹52,419.50 (paper account).
```

One more share and it is blocked, by the charges:

```
BLOCK #1 buy INFY.NS 42 shares, entry
        - loss: 42 shares x 10.70 risk a share = ₹449.40 at the stop, plus ₹51.51 charges = ₹500.91, over your ₹500 max loss per trade
```

## What it never does

It never places, edits or cancels an order, never changes the config, never
loosens a limit to let an order through, and never calls a BLOCK a PASS. A
PASS says the order fits the user's own rules; it is not a recommendation.

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
