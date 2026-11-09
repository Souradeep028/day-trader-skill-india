---
name: trader-analyst
description: >-
  The Results Analyst. Works through an Indian company's quarterly results from
  its own filing on the NSE or BSE (the Regulation 33 results PDF and the press
  release or investor presentation): revenue from operations, EBITDA, PAT and
  diluted EPS, quarter by quarter in India's April to March financial year (Q2
  FY27 is July to September 2026), each against the same quarter a year earlier
  and the quarter before, with margins and the last four quarters. Flags
  exceptional items and other income inside PAT. The figures come from the
  filing; results.py does the arithmetic exactly. Reads an earnings call
  transcript when you paste one. Records results dates (results.py event) in
  trader/events.json, so trader-risk keeps you out of a stock on its results
  day and the day after. Use when the user says "how were Infosys's results",
  "analyze the results", "what changed this quarter", "is revenue growing",
  "compare margins", "sum up the call", "when are the results", or before
  trading a stock around its results.
argument-hint: "[SYMBOL.NS]"
---

# trader-analyst

Results move stocks more than anything else on the calendar, and the press
release is written to put the best number first. This skill reads the
numbers the company filed with the exchange, lines them up quarter by
quarter, and says plainly what changed. For a day trader it answers two
questions before a trade: what did the last results say, and are the next
ones inside this trade?

Tool in this folder (standard library Python, no network, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/results.py" template INFY.NS > trader/results/INFY.json   # an empty file to fill
python3 "${CLAUDE_SKILL_DIR}/results.py" show trader/results/INFY.json                # growth, margins, TTM
python3 "${CLAUDE_SKILL_DIR}/results.py" show trader/results/INFY.json --metrics trader/metrics.json
python3 "${CLAUDE_SKILL_DIR}/results.py" show trader/results/INFY.json --json
python3 "${CLAUDE_SKILL_DIR}/results.py" event INFY.NS 2026-11-12 --what "Q2 FY27 results"   # trader/events.json
python3 "${CLAUDE_SKILL_DIR}/results.py" event INFY.NS 2026-10-28 --what "ex-dividend 21.50"     # an ex-date
python3 "${CLAUDE_SKILL_DIR}/results.py" event MARKET 2026-12-04 --what "RBI policy"             # the whole market
python3 "${CLAUDE_SKILL_DIR}/results.py" events                                             # dates from today on
```

`results.py event` records a results date (the board meeting date in the
company's NSE or BSE notice, filed days ahead) in `trader/events.json`.
trader-risk then blocks new entries in that stock on the date and the next
trading day, since results often come out after the 15:30 close. The
morning brief (trader-scanner's `morning.py`) lists the ones coming up.

Three kinds, told from the words when `--kind` is not given: **results**
(blocked that day and the next session), **ex-date** (ex-dividend, split,
bonus, rights: blocked that day only, since the open drops by it
mechanically) and **market** (symbol `MARKET`: RBI policy, the Union
Budget, election results, a big US release; every entry is blocked that
day). Add the month's RBI and Budget dates and the ex-dates of the stocks
you trade from the exchange's corporate actions page.

## Why the figures come from the filing

Every listed company files its quarterly results with the NSE and BSE within
45 days of the quarter's end (60 days for the fourth quarter, with the
audited year). No free feed carries those numbers reliably: Yahoo's
fundamentals for NSE stocks are patchy (Infosys's come back in US dollars,
HDFC Bank's quarters stopped in mid-2025 when checked on 8 November 2026),
and the exchanges' own sites turn away scripts. So the user opens the
filing, or pastes it or uploads the PDF here, and Claude reads the figures
from it into a results file. `results.py` then does the arithmetic, so no
growth rate is worked out by eye.

Where to find it: nseindia.com, the company's page, Corporate Filings >
Financial Results (or Corporate Announcements); bseindia.com has the same
filing; the company's investor relations page has the press release and
presentation.

## The results file

```json
{"symbol": "INFY.NS", "name": "Infosys", "basis": "consolidated", "unit": "crore",
 "source": "NSE filing, Financial Results, <date filed>",
 "quarters": [{"end": "2026-09-30", "revenue": 0, "ebitda": 0, "operating_profit": 0,
               "pat": 0, "eps": 0, "exceptional": 0, "other_income": 0}]}
```

- **basis**: consolidated (the group) or standalone (the company alone).
  Indian filings show both. Use consolidated unless the user asks, and never
  mix the two in one file.
- **unit**: the filing says it at the top of the table: crore, lakh, million
  or rupees. Copy the figures in that unit.
- **revenue**: revenue from operations, not total income (which adds other
  income). **pat**: profit after tax attributable to the owners of the
  company, the line EPS uses. **eps**: diluted EPS in rupees.
- **ebitda** and **operating_profit** are optional (banks and insurers have
  neither: leave them out, never put 0). **exceptional** and
  **other_income** are optional, to flag a profit the business did not earn.
- Put in at least the latest quarter, the one before it and the same quarter
  a year earlier; four quarters in a row give a TTM.

## The rules

- **Filed numbers first.** Every figure comes from the results filing. Use
  the press release or the call for context (guidance, segments, deal wins),
  and say which numbers came from where.
- **Compare like with like.** Growth is against the same quarter a year
  earlier. Quarter-on-quarter changes are shown, but many businesses are
  seasonal.
- **Flag one-offs.** When PAT is above operating profit, or there are
  exceptional items or a large other income, the tool says so: PAT is not
  all from the business.
- **Say what is missing.** No year-ago quarter in the file, no YoY. Not four
  quarters back to back, no TTM. Never estimate a quarter.

## Steps

1. Find the latest results filing for the company and read it (ask the user
   to paste or upload it if the site will not open).
2. Write the figures into `trader/results/<SYMBOL>.json` (start from
   `results.py template`) and run `results.py show` on it.
3. Write the summary: one line on the quarter (revenue, PAT, EPS, growth),
   one on margins, one on anything unusual (a one-off, a basis change), the
   source and its date. Plain words, real numbers, no adjectives the numbers
   do not support.
4. If the next results date (an NSE announcement of the board meeting, or
   `/trader-news`) falls inside the planned trade, say it before
   `/trader-exit` sets the stop: a stop does not protect against a gap.
   Record each date you find with `results.py event`, so the risk check
   holds the line.

## Example

The tests use a made-up company so every figure can be checked by hand
(`tests/fixtures/trader-analyst/example-results.json`): revenue 1,150 crore
in Q2 FY27, +15.0% on Q2 FY26 and +6.5% on Q1 FY27; PAT 144 crore, +20.0%;
EBITDA margin 21.0%; the last four quarters 4,380 crore of revenue and 615
crore of PAT. In Q4 FY26 its PAT of 210 crore held 75 crore of exceptional
items and was above operating profit, and the tool says both. A real summary
works the same way from the real filing.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company filed.
- Never invents a number the filing does not have, estimates a quarter, or
  takes a figure from a headline instead of the filing.
- Never mixes consolidated and standalone figures.

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
