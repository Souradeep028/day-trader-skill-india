#!/usr/bin/env python3
"""Connect a trading folder to your Zerodha Kite account, in one command, read-only. Standard library only.

    mkdir -p ~/trading && cd ~/trading
    python3 ~/day-trader/trader-broker/kite_setup.py                    # set up, log in, check
    python3 ~/day-trader/trader-broker/kite_setup.py --save kite-sample.json   # also keep Kite's answers, redacted

It does, in order:
  1. the folder   NSE's lists and a PAPER config (trader/config.json), as `trader.py setup` does. A config that is
                  already there is left exactly as it is.
  2. your app     the Kite Connect app's API key and secret (developers.kite.trade): KITE_API_KEY and KITE_API_SECRET
                  from the environment, else typed here (the secret is not shown). The secret is never written down.
  3. the login    opens Kite's login page; after you log in, Kite sends the browser to your app's redirect URL. Paste
                  that whole address (or just its request_token) here. Today's session goes to
                  trader/state/kite_session.json, readable by you only, and expires tomorrow morning. Skipped when
                  today's session is already there (--relogin to log in again).
  4. the check    Kite's profile, positions, orders and trades, read only: whether NSE, intraday (MIS) and SL-M
                  orders are open to this account, and what is held and pending now. --save FILE keeps Kite's
                  answers with your name, user id, e-mail and other personal fields taken out, for the pack's tests.
Nothing here places, changes or cancels an order. The config stays on paper; going live is your own edit.
Not financial advice.
"""

import argparse
import getpass
import json
import os
import re
import sys
import urllib.parse
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import guardian  # noqa: E402
import scheduler  # noqa: E402

check, ticket = guardian.check, guardian.ticket
LOGIN = "https://kite.zerodha.com/connect/login?v=3&api_key=%s"
PERSONAL = {"user_id", "user_name", "user_shortname", "email", "avatar_url", "placed_by", "pan", "phone", "mobile",
            "bank_account", "account_id", "dp_id", "user_type", "broker", "meta"}


class SetupError(Exception):
    pass


def request_token_of(text):
    """The request_token from what was pasted: the whole redirect address, or the token itself."""
    text = (text or "").strip()
    if "://" in text or "?" in text or "request_token=" in text:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(text).query or text.split("?", 1)[-1])
        if (q.get("status") or [""])[0] not in ("", "success"):
            raise SetupError("Kite says the login did not succeed (status=%s): log in again" % q["status"][0])
        text = (q.get("request_token") or [""])[0]
    if not re.match(r"^[A-Za-z0-9]{8,64}$", text):
        raise SetupError("that is not a request_token: paste the whole address Kite sent the browser to after the "
                         "login (it has ?request_token=... in it)")
    return text


def redact(doc):
    """Kite's answers with the personal fields taken out, for a test fixture."""
    if isinstance(doc, dict):
        return {k: ("REDACTED" if k in PERSONAL else redact(v)) for k, v in doc.items()}
    if isinstance(doc, list):
        return [redact(x) for x in doc]
    return doc


def summary(profile, positions, orders, trades):
    """Plain lines about what this account can do and holds, with nothing personal in them."""
    p = profile or {}
    ex, prods, types = (set(map(str, p.get(k) or [])) for k in ("exchanges", "products", "order_types"))
    pos, ords, others = guardian.book(positions, orders)
    pend = guardian.pending(ords)
    nt = len(ticket._rows(trades, "data", "trades"))
    lines = ["Kite connection: OK (profile, positions, orders and trades all answered)"]
    for label, ok in (("NSE equity", "NSE" in ex), ("intraday (MIS)", "MIS" in prods),
                      ("SL-M stop-loss orders", "SL-M" in types), ("market orders", "MARKET" in types)):
        lines.append("  %-22s %s" % (label, "yes" if ok else "NO: the guardian needs it, check the account's "
                                                              "segments and order types in Kite"))
    lines.append("  open MIS positions     %s" % (", ".join("%s %s" % (ticket._str_num(v["qty"]), s)
                                                          for s, v in sorted(pos.items())) or "none"))
    lines.append("  other positions        %s" % (", ".join(others) or "none"))
    lines.append("  pending MIS orders     %d" % len(pend))
    lines.append("  trades today           %d" % nt)
    return lines, {"nse": "NSE" in ex, "mis": "MIS" in prods, "slm": "SL-M" in types, "market": "MARKET" in types}


def main(argv=None, ask=input, ask_secret=getpass.getpass, browser=webbrowser.open, kite_cls=None, now=None):
    ap = argparse.ArgumentParser(description="Connect this trading folder to your Kite account (read-only).")
    ap.add_argument("--relogin", action="store_true", help="log in again even if today's session is there")
    ap.add_argument("--save", metavar="FILE", help="also write Kite's answers, personal fields removed")
    ap.add_argument("--no-browser", action="store_true", help="print the login address instead of opening it")
    a = ap.parse_args(argv)
    kite_cls = kite_cls or guardian.Kite
    now = now or check.parse_now(None)
    today = check.to_ist(now).date().isoformat()
    try:
        print("1. The trading folder (%s)" % os.getcwd())
        scheduler.setup()
        cfg = guardian.load_cfg(os.path.join("trader", "config.json"))
        print("   config: %s account" % ("PAPER" if cfg.get("paper", True) else "LIVE"))

        print("2. The Kite Connect app")
        session = ticket.read_json(guardian.SESSION, "Kite session", required=False) or {}
        fresh = session.get("date") == today and session.get("access_token") and not a.relogin
        if fresh:
            api_key, token = session["api_key"], session["access_token"]
            print("   today's session is already in %s: no login needed" % guardian.SESSION)
        else:
            api_key = os.environ.get("KITE_API_KEY") or ask("   API key (developers.kite.trade, your app): ").strip()
            secret = os.environ.get("KITE_API_SECRET") or ask_secret("   API secret (not shown, not saved): ").strip()
            if not api_key or not secret:
                raise SetupError("the app's API key and secret are both needed (developers.kite.trade)")
            print("3. The login")
            url = LOGIN % urllib.parse.quote(api_key)
            if a.no_browser or not browser(url):
                print("   open this in your browser and log in:\n   %s" % url)
            else:
                print("   your browser is opening Kite's login page; log in there")
            pasted = ask("   paste the address Kite sent the browser to (it has request_token= in it): ")
            data = kite_cls(api_key, None).session(request_token_of(pasted), secret)
            secret = None
            token = data["access_token"]
            guardian.save_session(guardian.SESSION, api_key, token, now)
            print("   today's session saved to %s (you only); it expires tomorrow morning" % guardian.SESSION)

        print("4. The check (read only)")
        k = kite_cls(api_key, token)
        answers = {"profile": k.profile(), "positions": k.positions(), "orders": k.orders(), "trades": k.trades()}
        lines, caps = summary(answers["profile"], answers["positions"], answers["orders"], answers["trades"])
        for line in lines:
            print("   " + line)
        if a.save:
            ticket.write_json(a.save, {"saved": today, "note": "Kite's answers, personal fields removed",
                                       "answers": redact(answers)})
            print("   Kite's answers, without your personal details, saved to %s" % a.save)
        print("")
        print("Done. Nothing was ordered, changed or cancelled. Next, in a session (Monday 9:15 to 15:30 IST):")
        print("   python3 %s run 2>&1 | tee guardian-dryrun.log      # the dry run: reads, prints, sends nothing"
              % os.path.join(HERE, "guardian.py"))
        print("Send the summary above (and that log) to Claude; never the API key, secret or token.")
        return 0 if all(caps.values()) else 1
    except (SetupError, guardian.GuardianError, guardian.KiteError, ticket.TicketError) as e:
        print("kite_setup: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
