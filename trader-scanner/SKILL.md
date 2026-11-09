---
name: trader-scanner
description: >-
  Chart Scanner. Scans hundreds of NSE charts for setups: opening-range
  breakouts, flags, oversold bounces. Builds the list of NSE stocks you can
  trade intraday (the EQ series) from NSE's own equity list, with each
  stock's price band (2, 5, 10 or 20 %, or none for F&O stocks) from NSE's
  band list for the risk check, runs a pre-market scan of daily closes
  (20-day closing-high breakout, bull flag, reclaimed the 20-day average,
  oversold, oversold bounce) and an intraday opening-range breakout scan on
  5-minute bars from the 9:15 open, then writes trader/watchlist.json for
  trader-news and trader-entry. Ships the Nifty 50 and Nifty Bank lists.
  Free Yahoo data, bar by bar. Use when the user says "scan the market",
  "find me setups", "what is breaking out", "opening range breakouts",
  "scan the Nifty 50", "scan for bull flags", "oversold stocks", "pre-market
  scan", "build my watchlist", "what should I look at today", or at the
  start of a trading day before /trader-news.
argument-hint: "[universe|daily|orb] [--watchlist FILE] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner reads hundreds of charts so the user does not have to,
and hands back a short list that matches a rule. A scan is a list of charts
that match a rule, not a list of stocks to buy: most breakouts fail, and day
trading loses most people money.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                       # -> trader/universe.txt + trader/bands.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --offline DIR         # from EQUITY_L.csv and sec_list.csv you saved
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --watchlist trader/nifty50.txt   # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols INFY.NS,TCS.NS,M\&M.NS
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/nifty50.txt     # after 9:35 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 09:40                 # cut the clock at 9:40 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Other flags: `--limit N`, `--min-price` (default 5 rupees), `--min-volume`
(default 1,000,000 shares), `--or-minutes` (15), `--max-confirm` (200),
`--now "2026-11-05 09:40"` (IST) to pin the clock, `--offline PATH` to read
saved Yahoo files, `--no-write`, `--json`. Copy the lists in this pack's
`trader/` folder (`nifty50.txt`, `banknifty.txt`) into the user's `trader/`
folder to scan them.

## What it reads and writes

| file | what |
| --- | --- |
| NSE's lists | `nsearchives.nseindia.com/content/equities/EQUITY_L.csv` and `sec_list.csv`, for `universe` |
| `trader/universe.txt` | written by `universe`: one `SYMBOL.NS|Name` a line |
| `trader/bands.json` | written by `universe`: each stock's price band, for trader-risk |
| `trader/nifty50.txt`, `trader/banknifty.txt` | the Nifty 50 (50) and Nifty Bank (14) stocks, from NSE's index files of 9 November 2026 |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 14:45) |
| `trader/watchlist.json` | written by `daily` and `orb`: the hits, read by trader-news and trader-entry |

## The universe

`scan.py universe` keeps every stock in NSE's EQ series, written the way
Yahoo names it (`INFY.NS`, `M&M.NS`, `BAJAJ-AUTO.NS`). BE and BZ are
trade-for-trade series, delivery only, so they cannot be day traded and are
left out; SME boards are out too. On 8 November 2026 that was 2,398 stocks of
2,632 rows. The same run writes each stock's price band: on that day 1,882
at 20 %, 170 at 10 %, 124 at 5 %, 1 at 2 %, and 218 F&O stocks with no fixed
band. NSE changes bands often, so run it each morning before trading.

NSE's site often turns away scripts. If the download fails, the tool says
how to do it by hand: the user saves the two CSV files from a browser into
one folder, then `scan.py universe --offline <that folder>`.

Scanning all 2,398 takes a while (120 spark requests, about a minute). The
Nifty 50 list is the sensible daily default: the most liquid stocks, the
tightest spreads.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request. Liquidity
comes from the spark meta: last price at least `--min-price` and last
session's volume at least `--min-volume`. Run it before the open or after
the close: during the session the volume is the day's so far, and today's
daily bar is left out until 3:30 PM. A special session such as the Diwali
Muhurat hour is a real bar and counts.

The setups are computed from daily closes only (spark has no highs or lows):

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Hits are listed most traded first (price times volume). After a broad rally
most of the list can be breakouts at once: say that, it means the whole
market moved, not that 30 stocks each have an edge.

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) after the 9:15 open: the
9:15, 9:20 and 9:25 five-minute bars. A hit is the first completed 5-minute
bar to CLOSE above the range high, at that bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. Stage 2 checks at most `--max-confirm` (200) of them, the
   most traded first (price times volume so far), and prints PARTIAL with the
   count it skipped. `--max-confirm 0` checks them all.

A chart is usable only when every 5-minute bar from 9:15 to the cutoff is
there; a missing bar means a halt or a thin stock. Null closes are dropped,
a bar off the 5-minute grid (the live tick, like 10:04:45) is dropped, and a
bar counts only once it is complete (start + 5 minutes). Before 9:35 AM, on a
weekend or NSE holiday, after the close, or after `no_entries_after` (an
expired setup) it prints one plain line and writes nothing.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag the
  exchange. Bars are 5-minute bars, bar by bar, never tick by tick. query2
  first, query1 on a 429 or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 50 of 51 charts (1 failed or
  had no data)". Never round a partial scan up to "the whole market".
- NSE trading holidays for 2026 (the NSE holiday circular; the NSE has no
  early closes).

## Steps

1. Each morning: `scan.py universe` (the price bands change).
2. Before the open: `scan.py daily --watchlist trader/nifty50.txt`. Tell the
   user the counts (fetched, liquid, scanned, hits) and the top few by setup.
3. After 9:35 AM IST: `scan.py orb --watchlist trader/nifty50.txt`. Tell the
   user how many charts broke out, with the time, the price and the range.
4. The user picks one. Hand it to `/trader-news SYMBOL.NS`, then
   `/trader-entry SYMBOL.NS`.

## Example: the reel

Thursday 5 November 2026, the Nifty 50 at 9:40 AM:

```
Opening-range breakout scan, Thu 2026-11-05 9:40 AM IST: 15-minute range (9:15 to 9:30 AM), 5-minute bars, Yahoo.
Fetched 50 of 51 charts (1 failed or had no data); 50 usable; 15 candidates, 15 confirmed with real highs and lows.
6 of 50 charts closed above their opening-range high by 9:40 AM:
   9:35 AM  BSE.NS           ₹3,745.30  range 3,655.40 to 3,745.00  +0.01% over
   9:35 AM  TATACONSUM.NS    ₹1,074.00  range 1,066.70 to 1,073.20  +0.07% over
   9:35 AM  TECHM.NS         ₹1,822.00  range 1,807.90 to 1,820.70  +0.07% over
   9:40 AM  BAJAJFINSV.NS    ₹1,803.00  range 1,784.10 to 1,799.30  +0.21% over
   9:40 AM  INFY.NS          ₹1,160.50  range 1,144.90 to 1,154.70  +0.50% over
   9:40 AM  SUNPHARMA.NS     ₹1,810.60  range 1,769.50 to 1,802.30  +0.46% over
```

By 10:30 AM it is 23 of 50. The user picks INFY.NS and Claude hands it to
`/trader-news INFY.NS`, then `/trader-entry INFY.NS`.

## What it never does

It never places an order, never calls a hit a buy, never scans a series that
cannot be traded intraday, and never presents a partial scan as complete.

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
