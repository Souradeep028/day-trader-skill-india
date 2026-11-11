# The day-trader pack in one container: the scheduler runs the NSE trading day on paper, in India time.
#   docker compose -f docker/compose.yaml up -d --build      (from the repo root; see the README, "Run it in Docker")
# The code is baked into the image; your trading folder (config, frozen tests, broker log, saved bars, reports)
# is mounted at /data from your machine, so it survives rebuilds and stays yours. Standard library only.
# The image only builds when the whole test suite passes on it (GitHub's CI is not running): broken code
# never reaches the daily run.
FROM python:3.12.8-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Kolkata

# the gate: the repo as it is, tests included, run offline on the recorded fixtures
FROM base AS test
WORKDIR /repo
COPY . .
RUN python3 -m unittest discover -s tests -q && touch /tmp/tests-passed

FROM base AS run
COPY --from=test /tmp/tests-passed /tmp/tests-passed
WORKDIR /app
COPY .claude-plugin/ .claude-plugin/
COPY trader/ trader/
COPY trader-alerts/ trader-alerts/
COPY trader-analyst/ trader-analyst/
COPY trader-broker/ trader-broker/
COPY trader-budget/ trader-budget/
COPY trader-entry/ trader-entry/
COPY trader-exit/ trader-exit/
COPY trader-news/ trader-news/
COPY trader-pelosi/ trader-pelosi/
COPY trader-risk/ trader-risk/
COPY trader-scanner/ trader-scanner/
COPY trader-timemachine/ trader-timemachine/

RUN useradd --create-home --uid 1000 trader && mkdir -p /data && chown trader /data
USER trader
WORKDIR /data
VOLUME ["/data"]

# healthy while the scheduler has checked in within the last 10 minutes
HEALTHCHECK --interval=5m --timeout=10s --start-period=1m \
  CMD python3 -c "import json,datetime as d;s=json.load(open('trader/state/scheduler.json'));t=d.datetime.strptime(s['alive'],'%Y-%m-%d %H:%M:%S');n=d.datetime.now(d.timezone.utc).replace(tzinfo=None)+d.timedelta(hours=5,minutes=30);exit(0 if (n-t).total_seconds()<600 else 1)"

ENTRYPOINT ["python3", "/app/trader-broker/scheduler.py"]
CMD ["run"]
