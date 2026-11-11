# The Day Trader skill

11 Claude skills for day trading: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a rupee.

For the Indian market: NSE stocks, IST, the NSE calendar, rupees, the NSE
tick and price bands, Zerodha's intraday charges, and orders in Zerodha
Kite's format, paper traded by default.

Free, MIT, no signup. The tools are plain Python that read free public data:
Yahoo Finance charts for NSE stocks, NSE's own equity and price band lists,
and Google News India.

One scans hundreds of charts for setups. Another tracks breaking news on
every stock you watch. One watches a setup bar by bar and tells you the moment
to get in, and another sets your stop and your target before you do, on the
NSE tick. The Time Machine replays the rest of the session 10,000 times from
the stock's own recent bars, plus every past day the same setup fired, and
tells you the honest odds, after charges.

Adapted for India from Jake Schincariol's
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT), which trades US stocks. The method and most of the code are his;
the NSE calendar, ticks, price bands, charges, Kite orders, paper trading,
Indian data sources and the tests on NSE data are this fork's.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default, and on paper the pack is
its own broker: nothing leaves your computer.

> **Looking for the original Trader skill?** Copy an investor's 13F, the
> stock screener, tax lots and rebalancing live in
> [github.com/Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill).
> This repo is its day-trading sequel: the Trader skill v2.

## Install

Paste this into Claude:

```
https://github.com/Souradeep028/day-trader-skill-india

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add Souradeep028/day-trader-skill-india
/plugin install day-trader-skill-india@day-trader-skill-india
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/day-trader-skill-india:trader-scanner` and so on. Copy the folders instead if you want
plain `/trader-scanner`:

```bash
git clone https://github.com/Souradeep028/day-trader-skill-india.git
cp -r day-trader-skill-india/trader-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. Copy `trader/nifty50.txt`, `trader/banknifty.txt` and
`trader/sectors.json` into your project's `trader/` folder: the lists to scan,
and the industries the risk check's sector cap reads. No Claude Code at all?
Paste any single `SKILL.md` at the top of a chat and it runs as a mode. You
lose the Python tools, but the method works.

The tools need Python 3.8 or newer. Nothing to pip install. Start your rules
from `trader-budget/config.example.json` (copy it to `trader/config.json`).

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Builds the NSE universe and its price bands, then scans for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Tracks Indian business news on every stock you watch and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target on the NSE tick before you enter, so you know when to get out. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a rupee: 10,000 simulated sessions plus every past time the setup fired, before and after charges. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits, charges and the stock's price band, and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade, charges included. |
| `/trader-broker` | Order Executor | Paper trades approved orders by default, or sends them to Zerodha Kite, never without your yes. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop, the flat-by time or the opening bell hits. |
| `/trader-analyst` | Results Analyst | Works through a company's quarterly results from its NSE filing and sums up what changed. |
| `/trader-pelosi` | Pelosi Tracker | Reads US Congress's stock-trade disclosures the minute they are filed (US stocks only). |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 15:10                                           (while it is open)
news / analyst                                                                 (on a schedule)
```

An example: Infosys, Thursday 5 November 2026, real data, on paper with the
example config (a 1 lakh budget, a 500 rupee max loss per trade).

1. **`/trader-scanner`** at 9:40 AM IST: of the Nifty 50, 6 charts had closed
   above their 15-minute opening range (9:15 to 9:30). INFY.NS is one: range
   1,144.90 to 1,154.70, the 9:35 candle closed at 1,160.50, 0.50 % over the top.
2. **`/trader-news`**: the morning's INFY headlines, in IST: an investor
   meetings notice, and stories on IT stocks lagging the day before. No
   results date inside the trade.
3. **`/trader-entry`** watched INFY bar by bar. At 9:39 it said waiting (the
   9:35 bar was still forming). At 9:40 it said triggered: the first
   5-minute close above the range, 1,160.50.
4. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, 1,149.80, so 10.70 a share at risk; target two times that risk,
   1,181.90, both on the 0.10 NSE tick; flat by 15:10 either way, before
   Zerodha squares off intraday positions itself.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times, each
   5-minute candle drawn from INFY's own candles at that time of day over the
   previous 57 sessions (seed 19, so anyone can repeat it): target first
   4.9 %, stop first 53.5 %, still open at 15:10 41.6 %. Average R -0.32, or
   -0.43 after charges. The same rule on those 57 real sessions fired 20
   times: 1 target, 10 stops, 9 neither. Its verdict: strongly toward the
   stop. That is the point of looking before you risk a rupee.
6. **`/trader-budget`**: 41 shares (₹47,580.50). If the stop fills at 1,149.80
   the loss is ₹438.70 plus ₹50.30 of charges, ₹489. 42 shares would be
   ₹500.91, over the limit. At the target it wins ₹826.27 after charges:
   reward:risk 1.69 after charges. With a 1R target the same trade is
   refused: after charges it wins ₹387.85 and loses ₹489.
7. **`/trader-risk`** checks it against your limits: the max loss with
   charges, the daily loss limit, trades per day, reward:risk after charges,
   the entry window (9:30 to 14:45), the price band, position size, cash and
   leverage. PASS or BLOCK, with the reason.
8. **`/trader-broker`** shows you the ticket in plain words and the Kite
   orders it stands for (an MIS market buy, then an SL-M stop and a LIMIT
   target). You type yes for that exact order, and on paper it fills at
   1,160.50 (₹20.05 of charges). On a live Kite account, Claude's own
   "Allow once / Deny" card asks you one more time.
9. **`/trader-alerts`** watches the stop, the target and 15:10. INFY ran to
   1,171.90 by 9:50 but reached neither side. At 15:10 the flatten ticket,
   a PASS and a second yes sold 41 at 1,164.90: 41 x 4.40 = ₹180.40, minus
   ₹50.69 of charges, **+₹129.71**. A small win on a setup the Time Machine
   called a loser: one day proves nothing either way.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 trader-scanner/morning.py                                      # the morning: lists, scan, results, news
python3 trader-scanner/scan.py universe                                # NSE's EQ series + price bands
python3 trader-scanner/scan.py surveillance asm.csv gsm.csv            # NSE's ASM/GSM lists, saved by hand
python3 trader-scanner/scan.py daily --watchlist trader/nifty50.txt    # pre-market setups
python3 trader-scanner/scan.py orb --watchlist trader/nifty50.txt      # opening-range breakouts so far
python3 trader-news/news.py --tickers INFY.NS --hours 12               # headlines, today's move
python3 trader-entry/entry.py check INFY.NS --setup orb                # waiting / triggered / expired
python3 trader-entry/entry.py watch INFY.NS --every 30                 # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 2R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-timemachine/edge.py run --watchlist trader/nifty50.txt  # which rules made money after charges
python3 trader-timemachine/swing.py run --watchlist trader/nifty50.txt # multi-day rules on 10 years of data
python3 trader-timemachine/rotation.py run --watchlist trader/nifty100.txt   # momentum and low-vol, vs real funds
python3 trader-timemachine/openclose.py run --watchlist trader/nifty50.txt   # day trades on 10 years of daily bars
python3 trader-timemachine/tom.py run                                  # the turn of the month vs holding the index
python3 trader-timemachine/archive.py save --watchlist trader/nifty50.txt   # keep the 5-minute bars Yahoo forgets
python3 trader-budget/sip.py status                                    # the index-fund core, XIRR, trading beside it
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-broker/ticket.py paper --index 1 --price 1160.50        # paper: fill it
python3 trader-broker/ticket.py paper-check                            # paper: stop or target hit?
python3 trader-broker/ticket.py reconcile --paper                      # no position without its stop
python3 trader-broker/ticket.py journal --paper                        # win rate, R, P&L after charges
python3 trader-broker/forward.py start --rule "orb30 short tight 2R low_cost+with_nifty"  # freeze a rule
python3 trader-broker/daily.py                                         # the day's report: market, forward test, you, tomorrow
python3 trader-broker/forward.py shadow                                # replay the rule on today's bars, every signal
python3 trader-broker/forward.py status                                # the forward test's verdict
python3 trader-broker/gapshadow.py start                               # freeze the gap-up short as a shadow test
python3 trader-broker/audit.py                                         # every paper and shadow trade on its chart, with costs
trader-broker/day.sh 11:00                                             # the autopilot for a stretch of the day, without cron
python3 trader-broker/ticket.py tax --fy FY27 --csv trader/tax.csv     # the year's P&L, charges, turnover
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by 15:10
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/results.py show trader/results/INFY.json        # quarters, growth, margins
python3 trader-analyst/results.py event INFY.NS 2026-11-12             # a results date: no entries that day
python3 trader-analyst/results.py event MARKET 2026-12-04 --what "RBI policy"   # no entries at all that day
python3 trader-pelosi/ptr.py index --member pelosi                     # her PTRs this year (US)
```

```bash
python3 -m unittest discover -s tests -v
```

GitHub Actions runs the same tests on every push (`.github/workflows/tests.yml`).

The tests run offline, on fixtures cut from real data: Infosys's 5-minute bars
for the 60 sessions to 6 November 2026, the Nifty 50's 5-minute and daily
charts, a real Yahoo spark answer, rows of NSE's equity and price band lists,
and real Google News India feeds. They reproduce every number in the example
above. The charges, sizes and P&L are worked by hand in the test comments,
and the scanner's and the Time Machine's results are checked against second
counts written separately in the tests.

## Does it make money?

Not yet, as far as the data shows. The Time Machine's `edge.py` tests 96
intraday rules (15- and 30-minute range breaks entered on the close or with a
stop order, a break of the previous day's high or low, VWAP pullbacks; long
and short; two stops; four targets) alone and with 15 filters, 1,536
combinations, over every Nifty 50 stock and session. Each trade is sized and
charged the way this pack trades, with a tick of slippage. A rule passes only
if it made money after charges in the earlier AND the later sessions, and by
more than luck explains across 1,536 tries (a session bootstrap, p under
0.05 / 1,536). It also says what one person would have made at 3 trades a
day, after 30 % tax, against simply holding Nifty 50.

Over the 58 sessions to 6 November 2026:

- **No rule passed.** 34 held up in both halves, none by more than luck.
- **Charges are what loses.** The pack's default (long 15-minute range
  break, midpoint stop, 2R) took 1,607 trades and lost ₹1,15,780, of which
  ₹1,06,901 was charges. For one person on ₹1,00,000 that is -12.5 % in 58
  sessions; holding Nifty 50 made +2.5 %. Dropping the trades whose charges
  were over 15 % of the risk cut the loss of the average rule from -0.28 R to
  -0.09 R a trade, so trader-budget and trader-risk now refuse those trades.
- **A stop entry gets a better price and loses more**: it also takes every
  wick through the range that falls back.
- **The best lead** is a short on the 30-minute range (low cost, Nifty also
  falling, or outside 10 to 11): about +0.1 R a trade, positive in both
  halves, but within luck (p 0.13 at best) and picked with hindsight.

**Swing trades** (held for days, in delivery) were tested on ten years of
Nifty 50 daily bars with `swing.py`: 40 rules, delivery charges, against
simply holding the same stocks. Nothing passed. The best made about 7.6 % a
year after tax with 5 positions; holding Nifty 50 made 10.6 % a year. On
this evidence an index fund beat every rule tested, intraday or swing.

**Day trades on ten years of data.** A trade that goes in at the open with a
fixed stop and is out at the close can be tested exactly on daily bars, so
`openclose.py` runs 200 such rules (fading or following gaps, with Nifty
filters) on ten years of the Nifty 50, charging the measured gap between
NSE's official open and close and the prices a 9:15-to-3:10 trade gets.
Entering at the first trade after 9:15, nothing passed. Entering in the
pre-open auction, one did (short a 2 % gap-up, 1 % stop: +0.28 R a trade over
2,275 trades), but it made only +0.11 R in the last third of the years and
lost on the last 60 days: a lead for a forward test that can place pre-open
orders, not an edge.

**The turn of the month** (holding NIFTYBEES only over each month's last and
first sessions, `tom.py`) is a real effect that does not pay: ₹1 lakh became
₹1.31 lakh after tax and charges in ten years, against ₹3.26 lakh holding.

**Factor portfolios** (momentum and low volatility, rebalanced monthly on the
Nifty 100) look spectacular in a backtest, and `rotation.py` shows why not to
believe it: today's Nifty 100 held equal-weight made 21.7 % a year against
10.3 % for the Nifty 50, which is survivorship, and no factor portfolio beat
that list by more than luck. The real funds settle it: the Nifty 200
Momentum 30 ETF made 8.9 % a year since August 2022 against 9.5 % for the
Nifty 50 ETF; the Nifty 100 Low Volatility 30 ETF made 11.9 % since May 2018
against 11.6 %, with a smaller worst fall.

**So the core is the index.** `sip.py` tracks a monthly SIP into a Nifty 50
fund and puts any trading's P&L beside it. Treat trading as a small,
separately measured experiment:

So: freeze one rule with `forward.py start`, paper trade every signal it
gives, and let `forward.py status` judge it by limits set before the first
trade (40 trades, an 8 R drawdown stop, the backtest's low end), and let
`forward.py shadow` replay it on every session after the close. Go live
only on a PASSED, at half size. The risk check's limits (cool-off, sector
cap, weekly and monthly losses, drawdown sizing, same-direction cap,
charges, big-Nifty-day halt, results days, ex-dates, market events,
surveillance) are there for the days it does not work.

## Hands off: what runs by itself

Set it up once, in your trading folder:

```bash
python3 trader-broker/forward.py start --rule "orb30 short tight 2R low_cost+with_nifty" \
    --expect-r 0.076 --expect-low -0.145                    # freeze the one rule to prove
crontab -e                                                  # then add the three lines below
```

```
CRON_TZ=Asia/Kolkata
40 8 * * 1-5         cd ~/trading && python3 /path/to/trader-scanner/morning.py >> trader/morning.log 2>&1
1-59/5 9-15 * * 1-5  cd ~/trading && python3 /path/to/trader-broker/autopilot.py run --once >> trader/autopilot.log 2>&1
50 15 * * 1-5        cd ~/trading && python3 /path/to/trader-broker/daily.py >> trader/daily.log 2>&1
```

`CRON_TZ` keeps the times in India time whatever the machine's clock says
(where your cron does not know `CRON_TZ`, set the machine to IST). The
autopilot runs a minute after each 5-minute bar closes, so Yahoo has posted
it; it only counts a bar once Yahoo's last trade is past the bar's close.

Then, with no input: the morning brief at 8:40 with the pre-flight's GO or
NO-GO at the top (calendar, config, the frozen rule, the data feed, anything
left over from yesterday, the loss limits left, market events; it also
records the day's limits, which hold all day), the frozen rule traded on
paper all day on a GO only (every risk check applied, stops and targets managed, flat by
15:10), and the daily report after the close, with the forward test's
verdict updated for the live lock. Set `"allow_short": true` in
trader/config.json for a short rule like this one.

**Without cron** (a Claude Code cloud session, a laptop terminal): `day.sh`
runs the same autopilot passes for a stretch of the day and stops at the
time you give it (only one runs at a time), so a scheduled task can start it
each morning and chain the stretches:

```bash
cd ~/trading && /path/to/trader-broker/day.sh 11:00          # pre-flight, then a pass every 5 minutes until 11:00
python3 /path/to/trader-timemachine/archive.py save          # after the close: keep the day's 5-minute bars
python3 /path/to/trader-broker/audit.py                      # the trade audit page: trader/audit/index.html
```

The trading folder (config, the frozen tests, the broker log, the saved bars,
the reports) is your own data: keep it out of the code. One way is its own
git branch, so a new machine can pick the run up where it stopped.

What still needs you, and how often:

- **Each live order, if it ever comes to that:** your yes in the chat, one
  order at a time. Never automated: a real-money order is your decision.
- **Weekly (5 minutes):** NSE's ASM and GSM lists, saved from the browser
  (`scan.py surveillance`). NSE's site turns scripts away.
- **Monthly (5 minutes):** the RBI policy date and the results and dividend
  dates of the stocks you trade (`results.py event`); the SIP purchase, or
  set up the broker's own ETF SIP and record it.
- **Yearly, each December:** next year's NSE holidays, and a look at the
  charges; the risk check and the reports remind you. Miss the holidays and
  nothing trades from 1 January: the pre-flight says NO-GO and the risk
  check refuses every new entry until the list is in.

## How it keeps intraday losses small

Nothing can promise no losing trades. What the pack does is make losing real
money hard:

- **Paper first, locked.** A live account (`"paper": false`) cannot enter a
  trade until a frozen rule has PASSED its forward test on both the
  automatic shadow replay and your own paper trades, within 30 days. The
  lock is in the code, not the config.
- **Ramp, then stop by itself.** Live trading starts at 25 % size, goes to
  50 % and 100 % only after good blocks of 20 trades, and stops on a bad
  block or a 5 R fall.
- **Hard ceilings.** ₹500 a trade with charges (₹250 in a drawdown), ₹1,500
  a day, ₹3,000 a week, ₹6,000 a month, two losses in a row ends the day,
  ₹1,000 open on one side, two entries a sector.
- **No loosening on a bad afternoon.** A limit raised after the day's first
  check waits until the next day.
- **No bad-odds trades.** Charges over 15 % of the risk, results days,
  ex-dates, RBI and Budget days, a 3 % Nifty day, surveillance stocks, the
  price bands, entries after 14:45, out by 15:10.
- **No silent mistakes.** `ticket.py reconcile` finds a position without
  its stop, a leftover stop that would open the opposite trade, a double
  entry, a stop moved away.

What still gets through: a stop can fill well past its price in a gap or
not at all in a halt, orders placed outside the pack, and editing the code.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
SEBI's own study of intraday trading in the equity cash segment found that
about seven in ten individual traders lost money. You can lose money. Decide
for yourself, and talk to a SEBI-registered adviser if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. On a live account, set the Kite order tools to "Needs
approval" in Claude (Customize, Connectors, tool permissions), never "Always
allow", so Claude's own "Allow once / Deny" card is a second check. No
futures and options, margin or short selling unless you turn them on.

**Paper first.** `"paper": true` is the default in `config.example.json`, and
on paper `ticket.py` fills orders itself at the price you give it. Most
Indian brokers have no paper account. Going live means a self-hosted Kite MCP
server with your own Kite Connect app (Zerodha's hosted server leaves out
order tools); see `/trader-broker`. Keys live in its config, never in the chat.

**Charges are real.** Zerodha's equity intraday charges (zerodha.com/charges,
read 8 November 2026) come into every size, every max loss, every
reward:risk and every paper fill: brokerage 0.03 % or 20 rupees an order,
STT 0.025 % on the sell, exchange 0.00307 %, SEBI 10 rupees a crore, stamp
duty 0.003 % on the buy, and 18 % GST. Another broker's rates go in
`"charges"` in your config. Your contract note is the final word.

**Leverage and square-off.** Intraday (MIS) positions can borrow up to about
five times your cash. This pack borrows nothing unless you set
`intraday_leverage` and `"allow_margin": true`, and the max loss is always on
the full position. Zerodha squares off any MIS position still open at about
3:20 PM, for 50 rupees plus GST an order: the 15:10 flat-by gets out first.

**Price bands and ticks.** A stock at its upper or lower price band can lock,
and an order off the NSE tick is rejected. `/trader-scanner universe` reads
each stock's band from NSE each morning, `/trader-risk` blocks buys right
under the upper band, and `/trader-exit` puts every stop and target on the tick.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those
60 days never did: results, a circuit, a headline. Treat its odds as a
reality check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, a
stop order can fill well past its price. The max loss shown is the loss at
the stop price.

**The data is free and unofficial.** Yahoo Finance's endpoints are not an
official API: they can lag, change or rate-limit you, and they often send
NSE's last bars of the day empty. NSE's site often turns away scripts; the
scanner tells you how to save its two lists from a browser instead. Check
anything that matters against your broker before you act on it.

**Algo trading rules.** SEBI's 2025 framework for retail algorithmic trading
covers orders sent through a broker's API. This pack sends one order at a
time, each after your yes, but check your broker's own rules (such as a
registered static IP for API orders) before going live.

**Congress's trades are public, and American.** Members of the US Congress
must disclose trades within 45 days (the STOCK Act); the Pelosi skill reads
those public filings. They are about US stocks and say nothing about the NSE.

**Taxes.** In India, intraday equity trading is speculative business income,
taxed at your slab rate, and its losses can only be set off against
speculative income. File with ITR-3 and keep your contract notes. Nothing
here is tax advice: ask a chartered accountant.

## Files

```
trader-scanner/     SKILL.md, scan.py, morning.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py, edge.py, swing.py, rotation.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, sip.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, forward.py, daily.py, autopilot.py, preflight.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, results.py
trader-pelosi/      SKILL.md, ptr.py
trader/             nifty50.txt, nifty100.txt, banknifty.txt, sectors.json (NSE's index lists and industries)
tests/              unittest suite + offline fixtures
```

MIT. Made by [Jake Schincariol](https://opusjake.ai); adapted for the NSE by [Souradeep028](https://github.com/Souradeep028).
