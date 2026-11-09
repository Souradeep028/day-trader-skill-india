---
name: trader-alerts
description: >-
  Price Alerts. Rings when a price, a stop or the opening bell hits. Turns
  the trade plan into three alerts in one go (the stop, the target and "flat
  by close" at your flat-by time), plus above / below / stop / target levels,
  percent moves from the previous close, any time of day, and the NSE
  opening bell, each ringing once so you are not spammed. Knows NSE hours
  with the 2026 trading holidays (the NSE has no early closes), works out
  India time (IST) itself, fetches free delayed Yahoo quotes for NSE symbols
  one at a time, and writes trader/prices.json (with each previous close)
  for sizing, paper fills and the risk check's price-band test. Use when the
  user says "alert me when", "tell me if INFY hits", "watch my stop", "watch
  my trade", "remind me to close by 3:10", "is the market open", "when does
  the market open", "get me a price", or right after /trader-exit writes a
  plan.
argument-hint: "[SYMBOL.NS] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. On a live Kite account the stop-loss and the
target sit at Zerodha once the entry fills; on paper, `ticket.py paper-check`
fills them. The alerts tell the user what happened and when it is time to be
flat.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol INFY.NS --stop 1149.80
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol INFY.NS --target 1181.90
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:10 --label "flat by close" --before-close
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS.NS --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote INFY.NS --write          # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote INFY.NS ^NSEI --write    # with Nifty 50, for trader-risk's index check
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset infy.ns-plan-stop
```

`--now "2026-11-05 15:10"` pins the clock (IST); `--offline DIR` reads saved
quote files instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes and previous closes, merged in, for trader-budget, trader-risk and trader-broker's paper fills |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:10") on a trading day | once per trading day |
| `opening_bell` | the NSE is open | once per trading day |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. A `time` rule
with `"before_close": true` would move up on an early close, but the NSE has
none, so "flat by close" at 15:10 always rings at 15:10.

A plan for a short (side sell) makes its stop alert ring at or ABOVE the
stop and its target alert at or below the target.

## Quotes

- Yahoo: `query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>` (like
  `INFY.NS`, `M%26M.NS`), an unofficial free JSON endpoint, one request per
  symbol, half a second apart. On a 429 it waits and tries once more; on a
  429 or a 5xx after that it tries `query1`. It identifies itself as
  `day-trader-skill/2.0`.
- Free quotes are unofficial, delayed and can lag the exchange. They are fine
  for alerts and for checking a size, not for timing an order. The broker's
  price is the one that fills. Each price in `trader/prices.json` keeps its
  quote time and the previous close, and trader-risk only uses one under 15
  minutes old.
- On a live Kite account, `get_ltp` or `get_quotes` (read only) give the
  broker's own price; write it into `trader/prices.json` as
  `{"as_of": "...Z", "prices": {"INFY.NS": 1164.0}}`.

## The NSE clock

Regular session 9:15 a.m. to 3:30 p.m. IST (after a 9:00 to 9:08 pre-open
auction), closed on weekends and on these equity segment trading holidays
from the NSE holiday circular:

- 2026: Jan 15 (Maharashtra municipal elections), Jan 26, Mar 3 (Holi), Mar 26
  (Ram Navami), Mar 31 (Mahavir Jayanti), Apr 3 (Good Friday), Apr 14
  (Ambedkar Jayanti), May 1 (Maharashtra Day), May 28 (Bakri Id), Jun 26
  (Muharram), Sep 14 (Ganesh Chaturthi), Oct 2 (Gandhi Jayanti), Oct 20
  (Dussehra), Nov 10 (Diwali Balipratipada), Nov 24 (Guru Nanak Jayanti),
  Dec 25 (Christmas). Every weekday missing from six months of real 2026
  daily bars is on this list. The NSE has no scheduled early closes. The
  one-hour Diwali Muhurat session (Sunday 8 November 2026) is not in the
  clock: check NSE's circular for its time.

IST is UTC+5:30 all year (no daylight saving), worked out in the tool, so it
needs no time zone files and ignores the computer's own zone. For any other
year (2027 included, until the NSE publishes its list) it says the holiday
list is unknown.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the flat-by time.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. On paper, run trader-broker's
   `ticket.py paper-check` after each check: it fills the stop or the target
   when the price got there. Live, a stop or target alert means that order
   has probably filled at Zerodha: check `get_positions`, cancel the other
   one, and record the fill with trader-broker. "Flat by close" means close
   anything still open: trader-broker `flatten`, then trader-risk, then the
   user's yes. Never act for them.

## Example: the reel

INFY.NS on Thursday 5 November 2026: entry 1160.50 at 9:40 AM, stop 1149.80,
target 1181.90, flat by 15:10. `add --from-plan` writes `infy.ns-plan-stop`,
`infy.ns-plan-target` and `infy.ns-flat-by` for that day. A `check` at 9:46
reads the 9:45 quote, 1164.00, between the stop and the target:

```
NSE OPEN (2026-11-05 09:46 IST, regular session).
Nothing hit. 3 rule(s) watched today, INFY.NS ₹1164.00.
```

At 3:10 PM:

```
ALERT Flat by close: it is 15:10 IST (set for 15:10), the market closes at 15:30. If INFY.NS is still open, close it: trader-broker flatten --symbol INFY.NS builds the sell ticket, then trader-risk and your yes.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell`, the bell rings at 9:15.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

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
