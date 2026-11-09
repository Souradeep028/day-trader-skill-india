---
name: trader-news
description: >-
  News Monitor. Tracks Indian business news on every stock you watch and
  flags what moves it: Google News India headlines searched by company name
  (Moneycontrol, Economic Times, Business Standard, Mint, NDTV Profit and the
  rest), Yahoo Finance headlines, and how far each stock is from yesterday's
  close. Shows only what is new since the last check, newest first, in IST,
  tags each item (results, analyst, deal, capital, people, legal, risk,
  circuit or halt, regulatory, macro) and never repeats one. Points to the
  company's own NSE and BSE announcements for anything that matters. Use when
  the user says "any news on INFY", "why is this moving", "what's the news on
  my watchlist", "morning briefing", "pre-market news", "monitor the news",
  "did anything break", "check the announcements", or after /trader-scanner
  finds setups.
argument-hint: "[SYMBOL.NS ...]"
---

# trader-news

A chart can look perfect and still be about to gap on a results date, a
circuit, a SEBI order or a block deal. Before a setup becomes a trade, this
skill reads the headlines for every stock on the watchlist and tells you only
what is new. It also prints how far each stock has moved today, so a
headline and a move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                                 # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers INFY.NS TCS.NS --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-google --json             # Yahoo only
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| `trader/universe.txt` | company names to search for (trader-scanner's universe); without it, the bare symbol |
| Google News, India edition | `news.google.com/rss/search?q="Infosys"+when:2d&hl=en-IN&gl=IN&ceid=IN:en` |
| Yahoo Finance headline RSS | `feeds.finance.yahoo.com/rss/2.0/headline?s=INFY.NS&region=IN&lang=en-IN` (thin for most NSE stocks) |
| Yahoo Finance chart endpoint | the last price against yesterday's close (unofficial, can lag) |
| `trader/state/news.json` | what has been shown (kept 30 days) |

**Company announcements.** The company's own filings (results, board meeting
dates, record dates, block and bulk deals, credit ratings, SAST and insider
disclosures) are on nseindia.com and bseindia.com under Corporate
Announcements. Both sites turn away scripts, so this tool does not read
them. When a headline matters, open the announcement in a browser, or ask
the user to paste it, and read that before the headline.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story and read it. Quote the source and link it. Never
  summarise a paywalled story you could not read: say so.
- **The company's filing beats a headline.** If an NSE or BSE announcement
  and a headline disagree, the announcement is what the company told the
  exchange.
- **Say what it touches.** Every item lists the symbols it touches (by
  company name, by "NSE: INFY", or by the symbol in capitals as a word, like
  "TCS") and its tags. Items marked `!` touch your stocks and are tagged
  earnings, halt (an upper or lower circuit, an F&O ban, a suspension), legal
  or risk.
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next, and never repeat
  a headline's price prediction as if it meant something.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` item, anything tagged earnings,
   halt, legal, regulatory or risk, and every MOVING symbol first.
3. Write a short brief, per stock: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "results
   are on Thursday after the close: an overnight gap can jump past any stop").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: a results date inside the trade, a circuit, a SEBI order,
   an OFS or block deal. Never act on a headline alone.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before the open.

## Example: the reel

Thursday 5 November 2026, INFY.NS broke its opening range at 9:40 AM IST. A
check at 9:40 over the last 24 hours leads with "Infosys: 3 Key Takeaways for
Long-Term Investors" (7:22, Yahoo Finance), "Infosys schedules investor
meetings in Mumbai and Singapore for November 2026" (the evening before) and
two stories on IT stocks lagging on 4 November ("Global AI Stocks Fell On US
Midterm Verdict. So Why Did TCS And Infosys Fall Too?", NDTV Profit). The
brief: "No results or company news due today; yesterday's IT selloff is the
backdrop. Nothing here changes a day trade that is out by 15:10." Its second
quarter results were already out (the feed has brokers' notes on them from
2 November), so no results date sits inside the trade. When a stock has no news, say so rather than padding the
brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per symbol, with a pause
  between calls.

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
