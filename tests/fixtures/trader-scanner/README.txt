Scanner fixtures, all from real data fetched from Yahoo and NSE on 8 and 9 November 2026:
  wall5m-2026-11-05.json.gz      the 50 Nifty 50 stocks' real 5-minute charts for 5 November 2026 (v8
                                 chart JSON, cut to that day), plus Yahoo's real answer for an unknown symbol
  daily-nifty50-2026-11-06.json  the same 50 stocks' real daily charts, 6 August to 6 November 2026
  spark-3mo-live-2026-11-09.json Yahoo's real spark answer for 20 symbols (19 real, one made up), fetched
                                 at 7:30 AM IST on 9 November; its last bar is the Diwali Muhurat
                                 session of Sunday 8 November
  nse/EQUITY_L.csv, sec_list.csv rows cut from NSE's real equity list and price band list of 8 November
Tests that need a chart read mid-session build it from these real bars and say so.
