---
name: trader-broker
description: >-
  Order Executor. Paper trades every approved order by default, and on a live
  account sends it to Zerodha Kite, never without your yes. Shows each order
  as a plain ticket (a bracket shows its stop-loss, target and the max loss
  at the stop with charges) plus the exact Kite place_order calls: the MIS
  entry, then the SL-M stop-loss and the LIMIT target once the entry fills.
  Records your yes word for word. On paper it fills the order itself at the
  live price and fills the stop or the target when the price gets there, with
  P&L after charges. On a live account Claude's own Allow once / Deny card
  asks again. Builds the sell ticket that closes a day trade by your flat-by
  time, records fills and logs each trade's max loss so trader-risk can hold
  the daily limit. Covers a self-hosted Kite MCP server in Claude Desktop
  (keys in its config, never in chat) and setting order tools to "Needs
  approval". Use when the user says "place the order", "buy it", "execute",
  "paper trade it", "send it to Zerodha", "close my position", "flatten",
  "get me out", "connect Kite", "set up paper trading", or after /trader-risk
  passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. Every order needs, every time:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. On a live account only: **the user clicked Allow once** on Claude's own
   permission card, which appears because the order tool is set to "Needs
   approval".

No yes, no order. On paper (`"paper": true`, the default) the third gate is
not needed because nothing leaves this computer: `ticket.py paper` is the
broker.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper --index 1 --price 1160.50       # paper: fill it
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper-check --price INFY.NS=1164.90  # paper: stop or target hit?
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 41 --filled-price 1160.60
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol INFY.NS --side sell --qty 41 --price 1149.80
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol INFY.NS --qty 41
python3 "${CLAUDE_SKILL_DIR}/ticket.py" reconcile --paper        # every position has its stop, nothing orphaned
python3 "${CLAUDE_SKILL_DIR}/ticket.py" reconcile --positions pos.json --broker-orders ord.json   # live
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log                      # today's decisions and P&L after charges
python3 "${CLAUDE_SKILL_DIR}/ticket.py" journal --paper          # every closed trade: win rate, R, P&L
python3 "${CLAUDE_SKILL_DIR}/ticket.py" tax --fy FY27 --csv trader/tax-FY27.csv   # the year, as a worksheet
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, fills paper orders, builds the flatten ticket,
and keeps the log in `trader/state/broker.json`. `--now "2026-11-05 09:40"`
pins the clock (IST). Without `--price`, `paper` and `paper-check` use a
price in `trader/prices.json` under 15 minutes old (trader-alerts writes it:
`alerts.py quote INFY.NS --write`, or `alerts.py check`).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, failed and filled, with charges, each entry's max loss, and the open paper stop-loss and target |
| `trader/portfolio.json` | cash, shares and charges paid, updated by `paper`, `paper-check`, `fill` and `result --filled-qty` |
| `trader/prices.json` | a fresh price for paper fills |
| `trader/plan.json` | the flat-by time for `flatten` |

## Paper trading (the default)

Most Indian brokers, Zerodha included, have no paper account. This skill is
one: the same tickets, the same risk check and the same explicit yes, then
`ticket.py paper` fills the order at the price you give it.

- A market order fills at the price. A limit buy fills at its limit once the
  price is at or under it, and says "Not filled" until then.
- A bracket's stop-loss and target stay open as paper orders.
  `ticket.py paper-check` fills the stop-loss when the price is at or under
  it, at that price (an SL-M order fills at the market, so a gap fills worse
  than the stop), or the target at its limit, and cancels the other one.
- Charges come out of the paper cash exactly as Zerodha would charge them.
- After the stop, the target or the flatten sell, `log` and `paper-check`
  print the trade's P&L after charges.

Paper fills are only as good as the price you give them: use the live price
at that moment, not a better one.

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

On paper: `ticket.py paper` sells short and opens a buy-back stop and target;
`paper-check` buys back when the price rises to the stop or falls to the
target; `flatten` writes a BUY ticket for a short (`--qty N --short` when
the qty comes from the broker); the journal marks the trade short and
measures R from the stop overhead.

## The journal and the tax worksheet

- `ticket.py journal` pairs each day's fills into round trips (bought and
  sold back to flat) and prints every trade with its charges, net P&L and R
  (net over the planned risk, from the stop recorded with its yes), then the
  win rate, net P&L, average R, best and worst trade and the longest run of
  losses. `--paper` or `--live` keeps them apart, `--from` and `--to` pick
  dates. Under 30 trades it says the sample is too small to judge.
- It also prints the profit factor (rupees won / rupees lost), the payoff
  ratio (the average win / the average loss), the expectancy (the average net
  a trade) and the worst fall of the running net, in rupees and as a % of the
  budget. The daily report shows the same for the shadow, the paper trades
  and the gap-up shadow test.
- For the autopilot's paper trades it also prints the entry slippage: how
  far each fill was from its signal price, on average per share and in R,
  and in rupees in all (in `--json` as `entry_slippage`). That is the cost of
  acting a little after the signal, and a live account pays at least that.
- Review it every week. Go live only if the paper journal holds up over a
  month or two, and stop and look again whenever the losing runs get longer.
- `ticket.py tax --fy FY27` covers India's financial year (April 2026 to
  March 2027): live round trips only unless `--include-paper`, gross P&L,
  charges by type (brokerage, STT, exchange, SEBI, stamp duty, GST), net,
  and the turnover the tax-audit limits use for speculative trading (each
  trade's profit or loss added up without signs). `--csv` writes one row per
  trade. It is a worksheet, not tax advice: intraday equity is speculative
  business income, filed on ITR-3, and its losses only set off against
  speculative income. Check it against the broker's tax P&L and a CA.

## The pre-flight (preflight.py): GO or NO-GO before the open

```bash
python3 "${CLAUDE_SKILL_DIR}/preflight.py"          # before 9:15; the morning brief runs it too
```

| check | NO-GO when |
| --- | --- |
| calendar | (not a fault) a weekend, an NSE holiday or a special session like Muhurat trading; (a fault) a date in a year the NSE holiday list does not cover yet |
| config | trader/config.json does not read or has no budget; a live account's lock status is reported |
| rule | no frozen rule in trader/forward.json, a setup edge.py does not know, or a short without `allow_short` |
| data | Yahoo sends no Nifty 50 bars, or its last session is not the previous trading day |
| files | trader/state is not writable |
| leftovers | a position, an open paper stop or target, or an unclosed fill from an earlier day |
| limits | the week's or the month's loss limit has less left than one trade's max loss |
| events | a market event today (RBI policy, the Budget) with `skip_market_event_days`, or expiry with `skip_expiry_days` |

The result goes to trader/state/preflight.json and the day's limits to
trader/state/limits.json (trader-risk's morning snapshot). The autopilot
takes new trades only on today's GO, runs the pre-flight itself on its first
pass if nobody did, keeps managing open positions on a NO-GO, and holds the
morning's limits all day.

## The paper autopilot (autopilot.py)

Trades the frozen rule on PAPER all day with nobody at the keyboard. Each
pass, on the bars that have closed: open autopilot positions are checked
against their stop (first) and target, and closed at the 15:10 flat-by;
fresh signals of the rule (the last closed bar or the one before) are sized
exactly as the shadow test sizes them, run through every trader-risk check,
and filled on paper when they pass. The approvals (reply "autopilot
(paper)"), fills and charges go into the broker log and the paper portfolio,
so the journal, the daily report and the forward test's PAPER verdict use
them. On the 5 November 2026 data, with the account limits the shadow does
not model switched off, it takes the shadow's three trades to the paisa;
with the pack's limits, it takes the first and blocks the other two (cash,
charges).

**How close is paper to real?** What it models: the delay between the bar
and the order, the price at that moment, a tick of slippage on every market
order, gaps through the stop, the order of events inside a 5-minute bar, a
limit that has to trade through, Zerodha's exact charges, and every risk
check. What free data cannot show: the bid-ask spread and the order book
(how many shares sit at each price; a one-tick cost is right for Nifty 50
stocks at this size, not in a thin stock or a fast market), where your limit
would be in the queue, an SL-M that fills more than a tick past the stop in a
fast fall, a stock locked at its price band, and orders the broker rejects
(a stock not allowed for MIS, a freeze quantity). Kite's live quotes would
close most of these. Paper results are the upper end of what live trading
gets: read the journal's slippage and the daily report's execution gap,
and expect a little worse.

It refuses to run with `"paper": false`. A live order always needs the
user's own yes in the chat, one order at a time, and Kite's Allow once card.

Run it every 5 minutes on market days from the trading folder, a minute
after each bar closes, in India time (it exits at once outside 9:15 to 15:30
and on holidays):

```
CRON_TZ=Asia/Kolkata
1-59/5 9-15 * * 1-5  cd ~/trading && python3 /path/to/trader-broker/autopilot.py run --once >> trader/autopilot.log 2>&1
```

Built to run unattended:

- **Finished bars only**: a bar counts once it has closed by the clock AND
  Yahoo's last trade is past its close, so a half-posted bar never triggers
  an entry or a stop the backtest would not have seen.
- **Real fills**: an entry fills at the price when the pass runs (Yahoo's
  last trade), a tick worse, not at the signal bar's close; it is sized again
  to the same max loss, skipped if the price already ran past the stop or
  target, and the difference is kept on the position as slippage.
- **Exits minute by minute**: for a held stock it also reads Yahoo's
  1-minute bars. When a 5-minute bar reaches the stop or the target, the
  1-minute bars say which came first. The stop (an SL-M at the broker)
  fills at the stop, or at the open of the minute that gapped through it,
  a tick worse. The target (a resting LIMIT) only fills when the price
  trades THROUGH it by a tick: a bare touch does not fill an order queued
  behind others. Without 1-minute bars the 5-minute bar decides, stop
  first.
- **No trading on stale data**: when Yahoo's last trade is over 3 minutes
  old during the session, no entry is taken in that stock that pass (a
  WARNING, counted in the heartbeat); exits still follow the finished bars.
- **The flat-by at the price then**: the 15:10 close-out is a market
  order sent by that pass, so it fills at the price when the pass runs, a
  tick worse.
- **One pass at a time**: a lock file makes a second pass exit at once
  while a slow one is still running.
- **No bad prints**: a 5-minute bar 8 % or more away from the bar before,
  when the next bar is straight back, is replaced by a flat bar (it never
  traded); a jump like that on the latest bar waits one bar to be confirmed.
- **No position left blind**: a stock it holds gets a second fetch; while
  its data is missing every pass logs a WARNING and counts it in the
  heartbeat, and past the flat-by with still no data the position is closed
  at the last price seen, and flagged.
- **A heartbeat**: each pass records itself and any error in
  trader/state/autopilot.json. The daily report says how many passes ran,
  the last one, the failures, and whether it stopped before the flat-by;
  the next morning's pre-flight repeats it.

## The gap-up shadow test (gapshadow.py)

trader-timemachine's `openclose.py` found one rule that passed on ten years
of daily bars, but only for entries in NSE's pre-open auction: short a Nifty
50 stock that opened 2 % or more up, a 1 % stop, out at 15:10. It had faded
in the later years, and a rule picked from 200 flatters itself, so
`gapshadow.py` freezes it and judges it only on sessions after the freeze,
by forward.py's rules (40 trades, an 8 R drawdown stop, the backtest's low
end +0.147 R). Nothing is traded: it needs pre-open orders and prices. After
the close it works out what the rule would have done from Yahoo's daily bar
(the official open is the auction price) and the day's 5-minute bars (the
stop and the 15:10 exit), the 3 biggest gaps a day.

```bash
python3 "${CLAUDE_SKILL_DIR}/gapshadow.py" start       # freeze it today: trader/forward-gap.json
python3 "${CLAUDE_SKILL_DIR}/gapshadow.py" shadow      # replay every session since, after the close
python3 "${CLAUDE_SKILL_DIR}/gapshadow.py" status
```

The daily report replays it and adds a "Gap-up shadow test" section when
trader/forward-gap.json exists.

## The daily report (daily.py)

One page after the close, every trading day:

```bash
python3 "${CLAUDE_SKILL_DIR}/daily.py"                 # today, after 15:30 IST -> trader/daily/<date>.md
python3 "${CLAUDE_SKILL_DIR}/daily.py" --date 2026-11-11 --no-write
```

1. **The market**: Nifty 50 and India VIX on the day, how many of the
   watchlist rose and fell, the biggest movers.
2. **Forward test**: the frozen rule's shadow trades for the day (and any
   session since the start not yet replayed), the running totals and the
   verdict, saved to trader/forward.json for the live lock. Under the
   paper verdict: the entry slippage (per share, in R and in rupees), and
   the execution gap: the paper trades' average R against the shadow's, and
   on the trades both took, how much the paper fills gave up. A gap that
   grows means the backtest's fills are too kind.
3. **Your trading**: the day's round trips, paper and live apart, P&L after
   charges and R, any position still open after the close (it should not
   be), the losing streak, the week's and month's realised P&L against their
   limits, the drawdown, and whether a live account is locked, ramping or
   stopped.
4. **The core**: the SIP's value and XIRR (trader-budget's sip.py).
5. **Tomorrow**: the next session, Nifty's weekly expiry, results, ex-dates
   and market events, and upkeep that is due.

Run it where you trade to see parts 3 and 4: a scheduled task in the cloud
has the market and the forward test, but not your own log.

## The forward test: prove a rule on trades after it

A backtest picks the best of many tries, so it flatters. `forward.py` in
this folder freezes ONE rule and judges it on the trades taken after that,
by limits written down before the first one:

```bash
python3 "${CLAUDE_SKILL_DIR}/forward.py" start --rule "orb30 short tight 2R low_cost+with_nifty"  # reads trader/edge.json
python3 "${CLAUDE_SKILL_DIR}/forward.py" signal INFY.NS --taken            # log EVERY signal of the rule
python3 "${CLAUDE_SKILL_DIR}/forward.py" signal TCS.NS --skipped --reason "results tomorrow"
python3 "${CLAUDE_SKILL_DIR}/forward.py" status                            # trades, R, the verdict
```

- **start** saves the rule, what the backtest expects (its average R after
  charges and the low end of its 95 % bootstrap range, from `edge.py run
  --write`), and the stop rules: no verdict before 40 trades
  (`--min-trades`); STOP whenever the trades fall 8 R from their best
  (`--max-drawdown-r`); at 40, STOP if the average is 0 or less or under the
  low end, PASSED if above both.
- **signal** logs each signal, taken or skipped with a reason. Skipping more
  than a fifth of them means the test measures your picks, not the rule.
- **status** pairs the paper (or `--live`) fills since the start into round
  trips and flags trades off the plan: a loss over 1.25 R (a stop moved, or a
  fill far past it), no stop recorded, or a trade with no logged signal.

**The shadow test** runs the same rule with no human in it. After each close,
`forward.py shadow` replays the frozen rule on that session's real 5-minute
bars for the Nifty 50 (trader-timemachine's edge.py: the same trades,
charges and slippage as the backtest), takes the first 3 signals of the day
as one person would (`--per-day`), and stores them in trader/forward.json.
`--from DATE` fills in every session since the start (Yahoo keeps 60 days).
`status` prints the shadow next to your own trades: the gap between them is
your execution. Because it only replays bars, it can run on a schedule with
nothing to remember.

```bash
python3 "${CLAUDE_SKILL_DIR}/forward.py" shadow                    # today, after 15:30
python3 "${CLAUDE_SKILL_DIR}/forward.py" shadow --from 2026-11-10  # every session since
```

Every `status` and `shadow` run writes its verdicts into trader/forward.json,
and so does the daily report (daily.py), for the shadow AND the paper trades
(the autopilot's and your own), so the lock never waits for a manual run;
and trader-risk reads them: a live account can only enter trades when the
shadow replay AND your paper trades have both PASSED within the last 30 days.
Then live trades start at 25 % of the max loss, go to 50 % and 100 % after
each good block of 20, and stop by themselves after a bad block or a 5 R
fall (trader-risk's `live` check).

On a STOP, drop the rule: do not re-tune it on the same trades.

## Reconcile: what the broker holds against the plan

Kite has no bracket order for stocks, so the stop and the target are two
separate orders placed after the entry fills, and nothing links them. That
leaves gaps a plan cannot see: a stop rejected (say, a trigger beyond the
price band) leaves the position with no stop; a target that fills leaves its
stop waiting, and if that stop triggers later it opens the OPPOSITE position;
a timed-out place_order retried blindly buys twice.

`ticket.py reconcile` checks the broker's own view against today's approved
entries and prints OK, or a PROBLEM with the exact fix:

- **unprotected**: a position with no open stop-loss (the fix is the Kite
  place_order call for an SL-M at the plan's stop)
- **under- or over-protected**: stops for fewer or more shares than held
- **orphan**: a stop or target still open on a flat symbol (cancel_order)
- **duplicate**: the same entry (its dts tag) filled twice
- **stop moved away**: the stop is further from the entry than the plan's
- **past the flat-by** with an intraday position still open
- a still-pending entry is a note: do NOT place it again

Live, call Kite's read-only `get_positions` and `get_orders`, save each
answer as JSON, and pass `--positions` and `--broker-orders`; on paper it
reads the paper account. Run it after every fill, after every stop or target
fills, before any retry of an order that timed out, and at the flat-by.
Exit code 1 means fix something before anything else.

## Live: Zerodha Kite, self-hosted MCP server

Zerodha's server is github.com/zerodha/kite-mcp-server. Its hosted version
at mcp.kite.trade leaves out the order tools for safety, so placing orders
needs the self-hosted one, with your own Kite Connect app. Checked against
its README on 2026-11-08.

1. The user makes a Kite Connect app at developers.kite.trade and gets its
   API key and secret (check its current pricing there). SEBI's rules for
   retail API trading (2025) and Zerodha's own may ask for more, such as a
   registered static IP for orders: check developers.kite.trade before going
   live.
2. The user builds the server (it needs Go): `git clone
   https://github.com/zerodha/kite-mcp-server`, then `go build -o
   kite-mcp-server` in that folder.
3. In Claude Desktop: Settings > Developer > Edit Config, then add the block
   from `claude_desktop_config.example.json` with the full path to the binary
   and their own key and secret typed into that file:

```json
{
  "mcpServers": {
    "kite": {
      "command": "/full/path/to/kite-mcp-server",
      "env": {
        "APP_MODE": "stdio",
        "KITE_API_KEY": "type it here, in this file only",
        "KITE_API_SECRET": "type it here, in this file only",
        "EXCLUDED_TOOLS": "place_gtt_order,modify_gtt_order,delete_gtt_order,get_mf_holdings"
      }
    }
  }
}
```

   **Keys never go in the chat.** Never ask for them, never read this file
   back into the conversation, and if the user pastes one anyway, tell them
   to delete that message and make a new secret on developers.kite.trade.
   `EXCLUDED_TOOLS` hides tools this pack does not use.
4. Restart Claude Desktop. The server's `login` tool gives a Zerodha sign-in
   link; the user signs in in their browser, not in the chat. A Kite session
   ends every day, so expect to sign in again each morning.
5. **Set the tool permissions.** Customize > Connectors, open kite, and for
   each tool pick Always allow, Needs approval or Blocked:
   - `place_order`, `modify_order`, `cancel_order`: **Needs approval**. Never Always allow.
   - read-only tools (`get_profile`, `get_margins`, `get_positions`,
     `get_holdings`, `get_orders`, `get_trades`, `get_order_history`,
     `get_quotes`, `get_ltp`, `get_ohlc`, `get_historical_data`,
     `search_instruments`): Always allow is fine.
6. Check it: ask Claude for the Kite profile and margins (read only).
7. Only then may the user set `"paper": false` in `trader/config.json`,
   themselves.

In Claude Code instead of Claude Desktop, keep the order tools on ask in
`.claude/settings.json`: `"permissions": {"ask": ["mcp__kite__place_order",
"mcp__kite__modify_order", "mcp__kite__cancel_order"]}`, and never put an
order tool in `allow`.

**Brackets on Kite.** Zerodha has no bracket orders for stocks, so a bracket
here is three `place_order` calls, all `product: "MIS"` (intraday) on the
NSE: the entry; once it fills, an `SL-M` sell with `trigger_price` at the
stop; and a `LIMIT` sell at the target. They do not cancel each other: when
one fills, cancel the other with `cancel_order`, or it can sell shares you no
longer hold. Each call carries a `tag` (`dts` plus the order's fingerprint)
so the orders can be found in `get_orders`. Zerodha squares off any MIS
position still open at about 3:20 PM, for 50 rupees plus GST an order: the
15:10 flat-by gets out first.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Paper trade this order? Reply yes or no." (or
   "Place this order?" on a live account). One order per question. Never
   offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "haan", "yes to all", or a yes that names a
   different stock or amount). Ask again. Never turn a reply into a yes yourself.
4. Paper: `ticket.py paper --index N --price <the live price now>`.
   Live: only after "Recorded your yes", call `place_order` with exactly the
   entry call the ticket printed. Claude's card appears: Allow once or Deny.
   Then `ticket.py result --index N --status placed --broker-id <Kite order
   id> --filled-qty Q --filled-price P` (from `get_order_history`), or
   `--status denied`, or `--status failed` with `--note`. A failed order is
   not retried without a new check and a new yes.
5. Live, once the entry has filled: place the stop-loss and the target calls
   the ticket printed. They belong to the same yes; each one still gets its
   own Allow once.

## Steps: while it is open, and out by 15:10

0. After the entry fills and the stop and target are in: `ticket.py reconcile`.
   Again whenever either one fills, and before retrying any order.

- trader-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`, then `alerts.py check`).
- Paper: `ticket.py paper-check` after each `alerts.py check` fills the stop
  or the target when the price gets there.
- Live: when the stop-loss or the target fills, cancel the other one, then
  record the fill: `ticket.py fill --symbol INFY.NS --side sell --qty 41
  --price 1149.80`.
- By `flat_by` (15:10 IST), anything still open gets closed:
  1. Live: ask Kite how many shares are held: `get_positions` (read only).
  2. `ticket.py flatten --symbol INFY.NS --qty 41` writes a market sell to
     `trader/orders.json`.
  3. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one order, as above.
  5. Paper: `ticket.py paper --index 1 --price P` sells and cancels the open
     paper stop-loss and target. Live: cancel the open stop-loss and target
     first (`get_orders`, then `cancel_order`, Needs approval), then
     `place_order` with the ticket, then `result`.

## Example: the reel

Infosys, Thursday 5 November 2026, on paper:

```
ORDER 1 of 1, PAPER account (filled here, no broker)
  BUY 41 INFY.NS, market, intraday (MIS), good for today
  stop-loss 1149.80
  target 1181.90
  max loss about ₹489 with charges if the stop fills at 1149.80 (it can fill worse in a fast market)
  why: opening-range breakout on INFY.NS: 41 shares, bound by your ₹500 max loss per trade (41 x 10.70 = ₹438.70 at the stop + ₹50.30 charges = ₹489)
  READY: trader-risk PASS, waiting for your yes
  On your yes, ticket.py paper fills it at the live price. For a live account Claude
  would make these Kite place_order calls:
    entry (now): {"variety": "regular", "exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 41, "product": "MIS", "order_type": "MARKET", "validity": "DAY", "tag": "dts..."}
    stop-loss (after the entry fills): {... "transaction_type": "SELL", "order_type": "SL-M", "trigger_price": 1149.8 ...}
    target (after the entry fills): {... "transaction_type": "SELL", "order_type": "LIMIT", "price": 1181.9 ...}
```

The user replies "yes". `ticket.py paper --index 1 --price 1160.50`:
`PAPER BUY 41 INFY.NS at 1160.50 (₹20.05 charges)`. INFY ran to 1171.90 by
9:50 but never reached the 1181.90 target or the 1149.80 stop. At 15:10 the
flatten ticket, a PASS and a second yes sell 41 at 1164.90 (₹30.64 charges):
41 x 4.40 = ₹180.40, minus ₹50.69 charges, **+₹129.71**. Charges took more
than a quarter of the move.

## What it never does

- Never places an order without the gates above, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, secrets, passwords or a Kite login.
- Never switches `"paper"` to live. The user does that themselves.
- Never fills a paper order at a price better than the market's at that moment.
- Never promises a fill price: a market order and an SL-M stop fill at whatever the market gives.

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
