---
name: trader-entry
description: >-
  Entry Timer. Watches a setup bar by bar and tells you the moment to get in.
  Reads free Yahoo 1-minute or 5-minute bars of NSE stocks, builds the
  opening range (the first 15 minutes from the 9:15 open by default) and
  calls the entry on the first completed bar that CLOSES above the range
  high, or above a level you give it (like a 20-day high from the scanner),
  with an optional volume check. Says waiting and how far, triggered with the
  time and the price, expired, or not open yet, in IST, and writes
  trader/entry.json for /trader-exit. Long side only. It never places an
  order. Use when the user says "tell me when to get in", "watch INFY", "has
  it broken out", "did it break the opening range", "entry signal", "time my
  entry", "check my watchlist", or after /trader-scanner and /trader-news,
  before /trader-exit.
argument-hint: "[SYMBOL.NS] [--setup orb|level] [--interval 5m|1m]"
---

# trader-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price, writes them down for
the Exit Planner, and stops. It never buys anything. Day trading loses most
people money, and a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check INFY.NS                # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check INFY.NS --write        # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check INFY.NS --setup level --level 1171.90
python3 "${CLAUDE_SKILL_DIR}/entry.py" check INFY.NS --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch INFY.NS --until 14:45  # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                    # every hit in trader/watchlist.json
```

Symbols are Yahoo's names for NSE stocks: `INFY.NS`, `M&M.NS`,
`BAJAJ-AUTO.NS`. Other flags: `--or-minutes N` (default 15), `--json`,
`--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`, or a
folder of `<SYMBOL>.json`), `--now "2026-11-05 09:40"` (IST) to replay a
day. `watch` with `--now` or `--offline` runs on a simulated clock and
replays the day bar by bar without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`), for `watchlist` |
| `trader/config.json` | `no_entries_before` (09:30) and `no_entries_after` (14:45), IST. Optional |
| Yahoo chart API | today's bars, `range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, setup, status, time, bar, price, level, opening range |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes after
  the 9:15 open: the 9:15, 9:20 and 9:25 five-minute bars, or fifteen
  1-minute bars. The 9:00 to 9:08 pre-open auction is not part of it. The
  level to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/trader-scanner`.
- **The trigger** is the first completed bar after the range that closes
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 9:35 bar closes at 9:40 AM). The
  price is its close, to the paisa.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 10:04:45, is never a bar. Yahoo
  often sends NSE's 15:15 to 15:25 bars empty; they are skipped.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. The 9:15 bar carries the
  opening auction, so 1.0 is already strict.
- **Time limits.** No entries before 9:30 (the range is complete) or after
  14:45 (config), so a trade has time before the 15:10 flat-by. The NSE has
  no early closes.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 9:15: not open yet. Before 9:30:
  the range is still forming. Weekends and NSE holidays: the market is closed.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/trader-scanner`. Check `/trader-news` for it first.
2. Run `entry.py check SYMBOL.NS`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL.NS --until 14:45` in
   the background, or `entry.py check SYMBOL.NS --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/trader-exit` to set the stop and the target before anything
   is bought.

## Example: the reel

INFY.NS, Thursday 5 November 2026, 5-minute bars, IST. The opening range is
1144.90 to 1154.70. At 9:39 the 9:35 bar is still open, so it waits:

```
WAITING  last close ₹1151.20 (9:30 bar), ₹3.50 under the range high ₹1154.70 (-0.30%).
The 9:35 bar is still forming (₹1160.50 so far, closes 9:40 AM); it does not count until it closes.
```

At 9:40 the bar has closed:

```
INFY.NS  Thu 2026-11-05 09:40 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 9:15-9:30: high ₹1154.70, low ₹1144.90 (3 of 3 bars).
TRIGGERED 9:40 AM at ₹1160.50: the 9:35 bar (bar 4) closed above the range high ₹1154.70 (+0.50%).
This is the moment to get in, if you take the trade. Nothing was placed.
```

So the entry is ₹1,160.50 at 9:40 AM, bar 4. With `--min-volume-x 1` it is
the same bar (1.15 times the range's average volume of 4,70,204 shares);
with `--min-volume-x 1.2` it never triggers that day.

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars. They are unofficial
  and can lag the exchange. Say so when a minute matters, and trust the
  broker's price over Yahoo's.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. v2 plans longs only. If the user wants a short, say that.
- One trigger per setup per day: the first close above. Later closes do not reset it.
- Never call an entry early because a bar looks like it will close above.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/trader-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/trader-broker` from a background
watch: nobody is there to say yes.

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
