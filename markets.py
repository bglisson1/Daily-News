#!/usr/bin/env python3
"""Market cards for Blake's Daily News.

Numbers come from free sources that do not need a key: Yahoo Finance chart
data, the U.S. Treasury daily yield curve, and Freddie Mac's weekly mortgage
survey. FRED's public CSV is used only when the Treasury or Freddie Mac file
does not return a fresh number.

A failed quote is marked unavailable. This module does not keep a previous
number and present it as current. The page build time is checked in the
browser; see page_is_stale for the rule.
"""

from __future__ import annotations

import csv
import html
import io
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

YAHOO_TIMEOUT = 12
RATE_TIMEOUT = 15
FRED_TIMEOUT = 10
MAX_BYTES = 2_000_000

# A daily print older than this is not shown. A week covers a long weekend
# and a holiday. Anything older is a stuck source, not a previous close.
DAILY_MAX_AGE = timedelta(days=7)
# Freddie Mac publishes once a week. Eighteen days allows one missed week.
MORTGAGE_MAX_AGE = timedelta(days=18)

# How old the page may be before the browser calls it out of date.
# Weekdays 8:00 AM–9:00 PM Eastern: 8 hours. The three main updates are
# about 5.5 hours apart, so one late run stays quiet and a missed run
# shows the banner. Overnight and weekends: 18 hours, because the gap from
# the 4:30 PM update to the next morning is about 13.5 hours and a skipped
# morning run needs a few more hours before the banner is fair.
DAY_START_HOUR = 8
DAY_END_HOUR = 21
DAY_LIMIT_HOURS = 8
OFF_LIMIT_HOURS = 18

YAHOO_KINDS = {"close", "futures", "live"}
RATE_KINDS = {"treasury", "mortgage"}
QUOTE_KINDS = YAHOO_KINDS | RATE_KINDS


@dataclass
class Quote:
    label: str
    symbol: str
    kind: str
    place: str
    ok: bool = False
    error: str | None = None
    price: float | None = None
    change: float | None = None
    pct: float | None = None
    bp: int | None = None
    as_of: datetime | None = None
    session_label: str = ""
    prefix: str = ""
    suffix: str = ""
    decimals: int = 2
    change_style: str = "percent"
    comparison: str = ""
    source_label: str = ""
    show_time: bool = False
    futures_symbol: str = ""


@dataclass
class MarketReport:
    glance: list[Quote] = field(default_factory=list)
    more: list[Quote] = field(default_factory=list)
    by_symbol: dict[str, Quote] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    enabled: bool = True

    def all_quotes(self) -> list[Quote]:
        return [*self.glance, *self.more]

    @property
    def outage(self) -> bool:
        rows = self.all_quotes()
        return bool(rows) and all(not quote.ok for quote in rows)


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def validate_markets(config: dict) -> None:
    """Stop the build when the market block is missing or mistyped.

    A quote that fails to download is not a config error. That card says
    unavailable and the rest of the page is still built.
    """
    if "markets" not in config:
        raise SystemExit(
            "feeds.yml is missing the markets: section. "
            "That section is the market cards at the top of the page. "
            "Put it back, or add markets: with enabled: no to hide the cards."
        )
    markets = config["markets"]
    if not isinstance(markets, dict):
        raise SystemExit(
            "markets: in feeds.yml must be a block of settings. "
            "Look at the lines under markets: and match their indentation."
        )
    if not as_bool(markets.get("enabled", True)):
        return
    titles = {"at_a_glance": "At a glance", "more": "More markets"}
    for key, title in titles.items():
        rows = markets.get(key)
        if not isinstance(rows, list) or not rows:
            raise SystemExit(
                f"markets: {key}: needs at least one card. "
                f"That list is the {title} row."
            )
        turned_on = 0
        for index, row in enumerate(rows, start=1):
            if _validate_row(key, index, row):
                turned_on += 1
        if turned_on < 1:
            raise SystemExit(
                f"markets: {key}: has no cards turned on. "
                f"Set enabled: yes on at least one {title} card, "
                "or set enabled: no under markets: to hide the whole box."
            )


def _validate_row(group: str, index: int, row) -> bool:
    """Return True when the card is turned on. Raise SystemExit when it is broken."""
    where = f"markets: {group}: card {index}"
    if not isinstance(row, dict):
        raise SystemExit(f"{where} is not a card. Copy one of the other cards.")
    label = str(row.get("label") or "").strip()
    if not label:
        raise SystemExit(f"{where} needs a label: line. That is the name on the card.")
    if not as_bool(row.get("enabled", True)):
        return False
    where = f"{where} ({label})"
    kind = str(row.get("kind") or "").strip().lower()
    if kind not in QUOTE_KINDS:
        raise SystemExit(
            f"{where} has kind: {kind or '(missing)'}. "
            "Use close, futures, live, treasury, or mortgage."
        )
    if kind in YAHOO_KINDS and not str(row.get("symbol") or "").strip():
        raise SystemExit(f"{where} needs a symbol: line, such as \"^GSPC\". Keep the quotes.")
    if kind in RATE_KINDS and not str(row.get("series") or "").strip():
        raise SystemExit(
            f"{where} needs a series: line, such as DGS10 or MORTGAGE30US."
        )
    if kind == "treasury" and not str(row.get("tenor") or "").strip():
        raise SystemExit(
            f"{where} needs a tenor: line, such as \"10 Yr\" or \"2 Yr\". Keep the quotes."
        )
    if "decimals" in row and row["decimals"] is not None:
        decimals = whole_number(row["decimals"])
        if decimals is None or not 0 <= decimals <= 6:
            raise SystemExit(f"{where} decimals must be a whole number from 0 to 6.")
    futures = row.get("futures")
    if futures not in (None, "") and not str(futures).strip():
        raise SystemExit(f"{where} futures: is blank. Paste a symbol or delete that line.")
    return True


def whole_number(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def fetch_markets(config: dict, now: datetime, getter=None) -> MarketReport:
    validate_markets(config)
    markets = config.get("markets") or {}
    report = MarketReport()
    if not as_bool(markets.get("enabled", True)):
        report.enabled = False
        report.log.append("  Markets are turned off in feeds.yml.")
        return report
    if getter is None:
        getter = http_get
    now = now.astimezone(EASTERN)

    rows: list[tuple[str, dict]] = []
    for place, key in (("glance", "at_a_glance"), ("more", "more")):
        for spec in markets.get(key) or []:
            if isinstance(spec, dict) and as_bool(spec.get("enabled", True)):
                rows.append((place, spec))

    kinds: dict[str, str] = {}
    for _place, spec in rows:
        kind = str(spec.get("kind") or "").strip().lower()
        if kind in YAHOO_KINDS:
            kinds[str(spec["symbol"]).strip()] = kind
    for _place, spec in rows:
        futures = str(spec.get("futures") or "").strip()
        if futures and futures not in kinds:
            kinds[futures] = "futures"

    fetched: dict[str, Quote] = {}
    if kinds:
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = [
                pool.submit(_fetch_one_yahoo, symbol, kind, now, getter)
                for symbol, kind in kinds.items()
            ]
            for job in as_completed(jobs):
                quote = job.result()
                fetched[quote.symbol] = quote
    report.by_symbol = fetched

    treasury_rows, treasury_error = _load_treasury(rows, now, getter)
    freddie_rows, freddie_error = _load_freddie(rows, getter)

    for place, spec in rows:
        kind = str(spec.get("kind") or "").strip().lower()
        if kind in YAHOO_KINDS:
            symbol = str(spec["symbol"]).strip()
            base = fetched.get(symbol)
            quote = _present_yahoo(base, spec, place)
        else:
            quote = _present_rate(
                spec,
                place,
                now,
                getter,
                treasury_rows,
                treasury_error,
                freddie_rows,
                freddie_error,
            )
        if place == "glance":
            report.glance.append(quote)
        else:
            report.more.append(quote)
        report.log.append(_log_line(quote))
    report.log.sort()
    if report.outage:
        report.log.append("  OUTAGE  every market card failed. The page will say so.")
    return report


def _fetch_one_yahoo(symbol: str, kind: str, now: datetime, getter) -> Quote:
    quote = Quote(label=symbol, symbol=symbol, kind=kind, place="")
    try:
        result = yahoo_chart(symbol, getter)
        meta = result.get("meta") or {}
        bars = daily_bars(result)
        live = meta.get("regularMarketPrice")
        stamped = None
        market_time = meta.get("regularMarketTime")
        if market_time:
            stamped = datetime.fromtimestamp(int(market_time), EASTERN)
        if kind == "close":
            parsed = cash_close(bars, now)
            if parsed is None:
                raise RuntimeError("not enough daily closes")
            price, change, pct, as_of = parsed
            quote.session_label = "Previous close"
            quote.change_style = "points"
            quote.show_time = False
            quote.price, quote.change, quote.pct, quote.as_of = price, change, pct, as_of
        elif kind == "live":
            parsed = live_print(bars, live, stamped, now)
            if parsed is None:
                raise RuntimeError("no live price")
            price, change, pct, as_of, fresh_print = parsed
            quote.change_style = "percent"
            quote.session_label = "Live" if fresh_print else "Last"
            quote.show_time = True
            quote.price, quote.change, quote.pct, quote.as_of = price, change, pct, as_of
        else:
            parsed = live_vs_prior(bars, live, now)
            if parsed is None:
                raise RuntimeError("not enough daily closes")
            price, change, pct, as_of, in_progress = parsed
            if in_progress and stamped is not None:
                as_of = stamped
            quote.change_style = "percent"
            quote.session_label = futures_session_label(now) if in_progress else "Last"
            quote.show_time = True
            quote.price, quote.change, quote.pct, quote.as_of = price, change, pct, as_of
        if not _fresh_enough(quote.as_of, now, DAILY_MAX_AGE) or quote.price is None:
            raise RuntimeError("quote is too old to show")
        quote.ok = True
        quote.source_label = "Yahoo Finance"
    except Exception as exc:
        quote.ok = False
        quote.error = _short_error(exc)
        quote.price = None
        quote.change = None
        quote.pct = None
        quote.session_label = ""
    return quote


def _present_yahoo(base: Quote | None, spec: dict, place: str) -> Quote:
    symbol = str(spec.get("symbol") or "").strip()
    if base is None:
        base = Quote(label=symbol, symbol=symbol, kind=str(spec.get("kind") or ""), place=place)
        base.error = "no quote"
    quote = Quote(
        label=str(spec.get("label") or symbol),
        symbol=symbol,
        kind=str(spec.get("kind") or "").strip().lower(),
        place=place,
        ok=base.ok,
        error=base.error,
        price=base.price,
        change=base.change,
        pct=base.pct,
        as_of=base.as_of,
        session_label=base.session_label,
        prefix=str(spec.get("prefix") or ""),
        suffix=str(spec.get("suffix") or ""),
        decimals=_decimals(spec),
        change_style=base.change_style or "percent",
        source_label=base.source_label,
        show_time=base.show_time,
        futures_symbol=str(spec.get("futures") or "").strip(),
    )
    if not quote.ok:
        quote.price = None
        quote.change = None
        quote.pct = None
    return quote


def _load_treasury(rows, now, getter):
    if not any(str(spec.get("kind") or "").lower() == "treasury" for _place, spec in rows):
        return None, None
    try:
        return treasury_yield_rows(now, getter), None
    except Exception as exc:
        return None, exc


def _load_freddie(rows, getter):
    if not any(str(spec.get("kind") or "").lower() == "mortgage" for _place, spec in rows):
        return None, None
    try:
        return freddie_mortgage_rows(getter), None
    except Exception as exc:
        return None, exc


def _present_rate(
    spec: dict,
    place: str,
    now: datetime,
    getter,
    treasury_rows,
    treasury_error,
    freddie_rows,
    freddie_error,
) -> Quote:
    kind = str(spec.get("kind") or "").strip().lower()
    series = str(spec.get("series") or "").strip()
    quote = Quote(
        label=str(spec.get("label") or series),
        symbol=series,
        kind=kind,
        place=place,
        suffix=str(spec.get("suffix") or "%"),
        decimals=_decimals(spec),
        change_style="bp",
        comparison="vs prior week" if kind == "mortgage" else "vs prior close",
    )
    observations = None
    source_label = ""
    primary_error = None
    if kind == "treasury":
        if treasury_rows is not None:
            try:
                observations = treasury_series(treasury_rows, str(spec.get("tenor") or "").strip())
                source_label = "U.S. Treasury"
            except Exception as exc:
                primary_error = exc
        else:
            primary_error = treasury_error or RuntimeError("Treasury file failed")
    else:
        if freddie_rows is not None:
            observations = freddie_rows
            source_label = "Freddie Mac"
        else:
            primary_error = freddie_error or RuntimeError("Freddie Mac file failed")

    limit = MORTGAGE_MAX_AGE if kind == "mortgage" else DAILY_MAX_AGE
    if observations and not _series_is_fresh(observations, now, limit):
        observations = None
        primary_error = RuntimeError("official file is too old")
        source_label = ""

    if observations is None:
        try:
            observations = fred_observations(series, getter)
            source_label = "FRED"
        except Exception as exc:
            detail = _short_error(primary_error) if primary_error else "no data"
            fallback = _short_error(exc)
            quote.error = f"{detail}; FRED fallback failed ({fallback})"
            quote.ok = False
            return quote
        if not _series_is_fresh(observations, now, limit):
            quote.error = "rate is too old to show"
            quote.ok = False
            return quote

    latest_dt, latest = observations[-1]
    quote.price = latest
    quote.as_of = latest_dt if latest_dt.tzinfo else latest_dt.replace(tzinfo=EASTERN)
    quote.source_label = source_label
    quote.session_label = "Weekly" if kind == "mortgage" else "Previous close"
    quote.show_time = False
    quote.ok = True
    if len(observations) >= 2:
        _prior_dt, prior = observations[-2]
        quote.change = latest - prior
        quote.bp = int(round((latest - prior) * 100))
        if prior:
            quote.pct = (latest - prior) / prior * 100.0
    return quote


def _decimals(spec: dict) -> int:
    if "decimals" not in spec or spec["decimals"] is None:
        return 2
    parsed = whole_number(spec["decimals"])
    return 2 if parsed is None else parsed


def _log_line(quote: Quote) -> str:
    if not quote.ok:
        return f"  FAIL {quote.label}: {quote.error or 'unavailable'}"
    price = format_price(quote)
    when = format_when(quote.as_of, quote.show_time)
    via = f" via {quote.source_label}" if quote.source_label else ""
    return f"  OK   {quote.label}: {price} {quote.session_label} {when}{via}".rstrip()


def _short_error(exc: BaseException | None) -> str:
    if exc is None:
        return "failed"
    text = str(exc).strip() or exc.__class__.__name__
    return text.splitlines()[0][:180]


def _fresh_enough(as_of: datetime | None, now: datetime, limit: timedelta) -> bool:
    if as_of is None:
        return False
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=EASTERN)
    delta = now - as_of
    if delta < -timedelta(days=1):
        return False
    return delta <= limit


def _series_is_fresh(observations, now: datetime, limit: timedelta) -> bool:
    if not observations:
        return False
    return _fresh_enough(observations[-1][0], now, limit)


def http_get(url: str, timeout: int, user_agent: str, accept: str = "*/*") -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": accept},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"could not connect ({reason})") from exc
    except TimeoutError as exc:
        raise RuntimeError("timed out") from exc


def yahoo_chart(symbol: str, getter=None) -> dict:
    if getter is None:
        getter = http_get
    encoded = quote(symbol, safe="")
    last_error: Exception | None = None
    for host in ("query1", "query2"):
        url = (
            f"https://{host}.finance.yahoo.com/v8/finance/chart/{encoded}"
            "?interval=1d&range=10d&includePrePost=true"
        )
        try:
            payload = getter(url, YAHOO_TIMEOUT, UA_BROWSER, "application/json,*/*")
            data = json.loads(payload.decode("utf-8", "replace"))
            chart = data.get("chart") or {}
            result = chart.get("result")
            if not result:
                err = chart.get("error") or {}
                raise RuntimeError(err.get("description") or "no data")
            return result[0]
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("Yahoo chart failed")


def daily_bars(result: dict) -> list[tuple[datetime, float]]:
    timestamps = result.get("timestamp") or []
    quote_block = (result.get("indicators") or {}).get("quote") or [{}]
    closes = quote_block[0].get("close") or []
    bars: list[tuple[datetime, float]] = []
    for stamp, close in zip(timestamps, closes):
        if stamp is None or close is None:
            continue
        bars.append((datetime.fromtimestamp(int(stamp), EASTERN), float(close)))
    return bars


def cash_close(bars, now: datetime):
    """Last completed regular session, and the change from the session before it."""
    usable = [(stamp, close) for stamp, close in bars if close is not None]
    if len(usable) < 2:
        return None
    last_stamp, _last_close = usable[-1]
    session_over = (now.hour, now.minute) >= (16, 0)
    if last_stamp.date() == now.date() and not session_over and len(usable) >= 3:
        usable = usable[:-1]
    if len(usable) < 2:
        return None
    level_stamp, level = usable[-1]
    _base_stamp, base = usable[-2]
    if not base:
        return None
    change = level - base
    return level, change, change / base * 100.0, level_stamp


def live_vs_prior(bars, live, now: datetime):
    usable = [(stamp, close) for stamp, close in bars if close is not None]
    if not usable:
        return None
    last_stamp, last_close = usable[-1]
    age_hours = (now - last_stamp).total_seconds() / 3600.0
    in_progress = 0 <= age_hours < 8 and len(usable) >= 2
    if len(usable) == 1:
        price = float(live) if isinstance(live, (int, float)) else float(last_close)
        return price, None, None, last_stamp, False
    if in_progress:
        _base_stamp, base = usable[-2]
        price = float(live) if isinstance(live, (int, float)) else float(last_close)
        as_of = last_stamp
    else:
        _base_stamp, base = usable[-2]
        price = float(last_close)
        as_of = last_stamp
    if not base:
        return price, None, None, as_of, in_progress
    change = price - base
    return price, change, change / base * 100.0, as_of, in_progress


def live_print(bars, live, stamped: datetime | None, now: datetime):
    """A live print versus the prior daily close.

    The card says Live when the print is less than 90 minutes old, and Last
    when the market has stopped updating. The as-of clock is the print time.
    """
    if not isinstance(live, (int, float)):
        return None
    if stamped is None:
        if not bars:
            return None
        stamped = bars[-1][0]
    age = (now - stamped).total_seconds()
    fresh_print = 0 <= age <= 90 * 60
    prior = [close for stamp, close in bars if stamp.date() < stamped.date()]
    if not prior and len(bars) >= 2:
        prior = [bars[-2][1]]
    if not prior or not prior[-1]:
        return float(live), None, None, stamped, fresh_print
    base = prior[-1]
    change = float(live) - base
    return float(live), change, change / base * 100.0, stamped, fresh_print


def futures_session_label(now: datetime) -> str:
    minutes = now.hour * 60 + now.minute
    if now.weekday() == 6 and minutes >= 18 * 60:
        return "Premarket"
    if now.weekday() >= 5:
        return "Last"
    if minutes < 9 * 60 + 30:
        return "Premarket"
    if minutes < 16 * 60:
        return "Live"
    return "After hours"


def fred_observations(series: str, getter=None) -> list[tuple[datetime, float]]:
    if getter is None:
        getter = http_get
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={quote(series, safe='')}"
    payload = getter(url, FRED_TIMEOUT, UA_BROWSER, "text/csv,*/*")
    return parse_two_column_csv(payload.decode("utf-8", "replace"))


def parse_two_column_csv(text: str) -> list[tuple[datetime, float]]:
    by_date: dict[datetime, float] = {}
    reader = csv.reader(io.StringIO(text))
    for index, row in enumerate(reader):
        if index == 0 or len(row) < 2:
            continue
        raw_date, raw_value = row[0].strip(), row[1].strip()
        if not raw_value or raw_value == ".":
            continue
        try:
            stamp = datetime.strptime(raw_date, "%Y-%m-%d").replace(tzinfo=EASTERN)
            by_date[stamp] = float(raw_value)
        except ValueError:
            continue
    rows = sorted(by_date.items())
    if not rows:
        raise RuntimeError("FRED file had no values")
    return rows


def treasury_yield_rows(now: datetime, getter=None) -> list[dict]:
    if getter is None:
        getter = http_get
    local = now.astimezone(EASTERN)
    months = [local.strftime("%Y%m")]
    previous = (local.replace(day=1) - timedelta(days=1)).strftime("%Y%m")
    if previous not in months:
        months.append(previous)
    collected: list[dict] = []
    last_error: Exception | None = None
    for ym in months:
        year = ym[:4]
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/"
            f"interest-rates/daily-treasury-rates.csv/all/{ym}"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
        )
        try:
            payload = getter(url, RATE_TIMEOUT, UA_BROWSER, "text/csv,*/*")
            text = payload.decode("utf-8", "replace")
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                if row.get("Date"):
                    collected.append(row)
        except Exception as exc:
            last_error = exc
    if not collected and last_error:
        raise last_error
    if not collected:
        raise RuntimeError("Treasury file had no rows")
    return collected


def treasury_series(rows: list[dict], tenor: str) -> list[tuple[datetime, float]]:
    by_date: dict[datetime, float] = {}
    for row in rows:
        raw_date = (row.get("Date") or "").strip()
        raw_value = (row.get(tenor) or "").strip()
        if not raw_date or not raw_value or raw_value == ".":
            continue
        try:
            stamp = datetime.strptime(raw_date, "%m/%d/%Y").replace(tzinfo=EASTERN)
            by_date[stamp] = float(raw_value)
        except ValueError:
            continue
    points = sorted(by_date.items())
    if not points:
        raise RuntimeError(f"no {tenor} values")
    return points


def freddie_mortgage_rows(getter=None) -> list[tuple[datetime, float]]:
    if getter is None:
        getter = http_get
    url = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
    payload = getter(url, RATE_TIMEOUT, UA_BROWSER, "text/csv,*/*")
    return parse_freddie_csv(payload.decode("utf-8", "replace"))


def parse_freddie_csv(text: str) -> list[tuple[datetime, float]]:
    reader = csv.DictReader(io.StringIO(text))
    by_date: dict[datetime, float] = {}
    for row in reader:
        raw_date = (row.get("date") or "").strip()
        raw_value = (row.get("pmms30") or "").strip()
        if not raw_date or not raw_value:
            continue
        try:
            stamp = datetime.strptime(raw_date, "%m/%d/%Y").replace(tzinfo=EASTERN)
            by_date[stamp] = float(raw_value)
        except ValueError:
            continue
    points = sorted(by_date.items())
    if not points:
        raise RuntimeError("Freddie Mac file had no 30-year rate")
    return points


def stale_limit_hours(now: datetime) -> int:
    """Hours the page may age before the out-of-date banner appears.

    Weekdays from 8:00 AM until 9:00 PM Eastern use 8 hours. Overnight and
    Saturday and Sunday use 18 hours. See the constants at the top of this file.
    """
    local = now.astimezone(EASTERN)
    minutes = local.hour * 60 + local.minute
    if local.weekday() < 5 and DAY_START_HOUR * 60 <= minutes < DAY_END_HOUR * 60:
        return DAY_LIMIT_HOURS
    return OFF_LIMIT_HOURS


def page_is_stale(built_at: datetime, now: datetime) -> bool:
    """True when this build is older than the schedule allows.

    Equal to the limit is still fresh. Only a strictly older page is stale.
    A build timestamp in the future is treated as fresh (clock skew).
    """
    if built_at.tzinfo is None:
        built_at = built_at.replace(tzinfo=EASTERN)
    age = now - built_at.astimezone(EASTERN)
    if age.total_seconds() < 0:
        return False
    return age > timedelta(hours=stale_limit_hours(now))


def stale_banner_text(built_at: datetime) -> str:
    local = built_at.astimezone(EASTERN) if built_at.tzinfo else built_at.replace(tzinfo=EASTERN)
    clock = local.strftime("%I:%M %p").lstrip("0")
    when = f"{local.strftime('%b')} {local.day}, {clock} ET"
    return f"This page is out of date: last updated {when}"


def format_price(quote: Quote) -> str:
    if quote.price is None:
        return "unavailable"
    if quote.decimals <= 0:
        body = f"{quote.price:,.0f}"
    else:
        body = f"{quote.price:,.{quote.decimals}f}"
    return f"{quote.prefix}{body}{quote.suffix}"


def format_abs_number(value: float, decimals: int) -> str:
    number = abs(value)
    if decimals <= 0:
        return f"{number:,.0f}"
    if number >= 1000:
        return f"{number:,.{decimals}f}"
    return f"{number:.{decimals}f}"


def format_when(stamp: datetime | None, show_time: bool) -> str:
    if stamp is None:
        return ""
    local = stamp.astimezone(EASTERN) if stamp.tzinfo else stamp.replace(tzinfo=EASTERN)
    day = f"{local.strftime('%a')}, {local.strftime('%b')} {local.day}"
    if not show_time:
        return day
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{day} · {clock} ET"


def move_display(quote: Quote) -> tuple[str, str]:
    if not quote.ok or quote.price is None:
        return "flat", "unavailable"
    if quote.change_style == "bp":
        if quote.bp is None:
            return "flat", "—"
        if quote.bp == 0:
            return "flat", "Unchanged"
        direction = "up" if quote.bp > 0 else "down"
        sign = "+" if quote.bp > 0 else "−"
        text = f"{sign}{abs(quote.bp)} bp"
        if quote.kind == "mortgage":
            text += " this week"
        elif quote.comparison:
            text += f" {quote.comparison}"
        return direction, text
    if quote.pct is None:
        return "flat", "—"
    if abs(quote.pct) < 0.005:
        return "flat", "Unchanged"
    direction = "up" if quote.pct > 0 else "down"
    sign = "+" if quote.pct > 0 else "−"
    percent = f"{sign}{abs(quote.pct):.2f}%"
    if quote.change_style == "points" and quote.change is not None:
        point_sign = "+" if quote.change > 0 else "−"
        points = format_abs_number(quote.change, 2 if quote.decimals else 0)
        return direction, f"{point_sign}{points} · {percent}"
    return direction, percent


def render_markets(report: MarketReport) -> str:
    if not report.enabled:
        return ""
    glance = "".join(
        render_quote(quote, report.by_symbol.get(quote.futures_symbol))
        for quote in report.glance
    )
    more = "".join(render_quote(quote, None) for quote in report.more)
    outage = ""
    if report.outage:
        outage = (
            '<p class="markets-outage">Market data is unavailable. '
            "No quote source responded, so no numbers are shown.</p>"
        )
    return (
        '<section id="markets" class="markets">'
        "<h2>Markets</h2>"
        f"{outage}"
        '<p class="markets-kicker">At a glance</p>'
        f'<div class="glance">{glance}</div>'
        '<p class="markets-kicker">More markets</p>'
        f'<div class="more-grid">{more}</div>'
        f'<p class="markets-src">{esc(source_footnote(report))}</p>'
        '<p class="stale-note">These market figures are from an old update. '
        "They are not current.</p>"
        "</section>"
    )


def source_footnote(report: MarketReport) -> str:
    used_fred = any(quote.ok and quote.source_label == "FRED" for quote in report.all_quotes())
    text = (
        "Stocks, futures, oil, gold, VIX, and Bitcoin: Yahoo Finance. "
        "Treasury yields: U.S. Treasury daily yield curve. "
        "Mortgage: Freddie Mac weekly survey."
    )
    if used_fred:
        text += " FRED filled in a rate whose official file did not answer."
    text += " A card that says unavailable did not get a fresh number."
    return text


def render_quote(quote: Quote, futures: Quote | None) -> str:
    chip = futures_chip(futures) if futures is not None else ""
    symbol = esc(quote.symbol)
    if not quote.ok:
        return (
            f'<article class="q is-missing" data-symbol="{symbol}" data-ok="no">'
            f'<p class="q-name">{esc(quote.label)}</p>'
            '<p class="q-price">unavailable</p>'
            '<p class="q-asof">No fresh quote</p>'
            "</article>"
        )
    direction, move = move_display(quote)
    arrow = {"up": "▲", "down": "▼", "flat": "–"}[direction]
    meta = _meta_line(quote)
    return (
        f'<article class="q {direction}" data-symbol="{symbol}" data-ok="yes">'
        f'<p class="q-name">{esc(quote.label)}</p>'
        f'<p class="q-price">{esc(format_price(quote))}</p>'
        f'<p class="q-move {direction}"><span aria-hidden="true">{arrow}</span> {esc(move)}</p>'
        f"{chip}"
        f'<p class="q-asof">{esc(meta)}</p>'
        "</article>"
    )


def _meta_line(quote: Quote) -> str:
    if quote.kind == "mortgage" and quote.as_of:
        local = quote.as_of.astimezone(EASTERN)
        bits = [f"Weekly · Week of {local.strftime('%b')} {local.day}"]
    else:
        bits = []
        if quote.session_label:
            bits.append(quote.session_label)
        when = format_when(quote.as_of, quote.show_time)
        if when:
            bits.append(when)
    if quote.source_label and quote.source_label != "Yahoo Finance":
        bits.append(quote.source_label)
    return " · ".join(bits)


def futures_chip(quote: Quote) -> str:
    if not quote.ok or quote.pct is None:
        return ""
    prefix = {
        "Premarket": "Premkt",
        "Live": "Live",
        "After hours": "After hrs",
        "Last": "Last",
    }.get(quote.session_label, "Fut")
    if abs(quote.pct) < 0.005:
        direction = "flat"
        text = f"{prefix} flat"
    else:
        direction = "up" if quote.pct > 0 else "down"
        sign = "+" if quote.pct > 0 else "−"
        text = f"{prefix} {sign}{abs(quote.pct):.2f}%"
    if len(text) > 18:
        return ""
    return f'<p class="q-fut {direction}">{esc(text)}</p>'


def render_stale_banner(built_at: datetime) -> str:
    return (
        '<div id="stale-banner" class="stale-banner" hidden role="status">'
        f"{esc(stale_banner_text(built_at))}"
        "</div>"
    )


def render_freshness_script(built_at: datetime) -> str:
    payload = {
        "built": built_at.astimezone(EASTERN).isoformat(),
        "dayStart": DAY_START_HOUR,
        "dayEnd": DAY_END_HOUR,
        "dayHours": DAY_LIMIT_HOURS,
        "offHours": OFF_LIMIT_HOURS,
    }
    data = json.dumps(payload, separators=(",", ":"))
    return (
        f'<script id="freshness-data" type="application/json">{data}</script>\n'
        f"<script>{FRESHNESS_JS}</script>"
    )


# The numbers (8 hours, 18 hours, 8:00 AM, 9:00 PM) are not written here.
# The script reads them from the JSON tag, which is filled from the constants
# above, so the browser check matches page_is_stale.
FRESHNESS_JS = r"""
(function () {
  try {
    var node = document.getElementById("freshness-data");
    if (!node) return;
    var data = JSON.parse(node.textContent);
    var built = new Date(data.built);
    if (isNaN(built.getTime())) return;
    var now = new Date();
    var fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23"
    });
    var map = {};
    fmt.formatToParts(now).forEach(function (part) { map[part.type] = part.value; });
    var hour = parseInt(map.hour, 10);
    if (hour === 24) hour = 0;
    var minute = parseInt(map.minute, 10);
    var minutes = hour * 60 + minute;
    var isWeekday = map.weekday === "Mon" || map.weekday === "Tue" || map.weekday === "Wed" || map.weekday === "Thu" || map.weekday === "Fri";
    var daytime = isWeekday && minutes >= data.dayStart * 60 && minutes < data.dayEnd * 60;
    var limitHours = daytime ? data.dayHours : data.offHours;
    if (now.getTime() - built.getTime() > limitHours * 3600000) {
      var banner = document.getElementById("stale-banner");
      if (banner) banner.hidden = false;
      var markets = document.getElementById("markets");
      if (markets) markets.classList.add("is-stale");
    }
  } catch (err) {
    return;
  }
})();
"""


MARKET_CSS = """
  .stale-banner {
    background: #c00;
    color: #fff;
    text-align: center;
    font-weight: 700;
    font-size: 16px;
    line-height: 1.35;
    padding: 10px 14px;
  }
  .stale-banner[hidden] { display: none !important; }
  .markets {
    border: 2px solid #000;
    margin: 4px 0 16px;
    padding: 8px 8px 10px;
  }
  .markets h2 {
    margin: 0 0 4px;
    font-size: 15px;
    letter-spacing: 1px;
    text-align: center;
    text-transform: uppercase;
  }
  .markets-kicker {
    margin: 8px 0 4px;
    font-size: 12px;
    letter-spacing: 1px;
    text-align: center;
    text-transform: uppercase;
  }
  .markets-outage {
    margin: 6px 0;
    color: #c00;
    font-weight: 700;
    text-align: center;
    font-size: 15px;
  }
  .glance, .more-grid {
    display: grid;
    gap: 6px;
  }
  .glance { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .more-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); }
  .q {
    border: 1px solid #000;
    padding: 5px 4px 4px;
    text-align: center;
    min-width: 0;
  }
  .q-name {
    margin: 0;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.2px;
    overflow-wrap: anywhere;
  }
  .q-price {
    margin: 1px 0;
    font-size: 18px;
    font-weight: 700;
    line-height: 1.15;
    overflow-wrap: anywhere;
  }
  .q-move, .q-fut, .q-asof {
    margin: 0;
    font-size: 12px;
    line-height: 1.25;
    overflow-wrap: anywhere;
  }
  .q-fut { margin-top: 1px; }
  .q-asof { color: #333; margin-top: 2px; }
  .q.down .q-move, .q.down .q-fut { color: #c00; }
  .q.up .q-move, .q.up .q-fut { color: #000; }
  .q.is-missing {
    border-style: dashed;
    background: #fff5f5;
  }
  .q.is-missing .q-price {
    color: #c00;
    font-size: 14px;
    font-weight: 700;
  }
  .markets-src, .stale-note {
    margin: 8px 0 0;
    font-size: 12px;
    line-height: 1.35;
    text-align: center;
    color: #333;
  }
  .stale-note { display: none; }
  .markets.is-stale {
    box-shadow: inset 0 0 0 3px #c00;
  }
  .markets.is-stale .stale-note {
    display: block;
    color: #c00;
    font-weight: 700;
  }
  .markets.is-stale .q { background: #f4f4f4; }
  .markets.is-stale .q-price::after {
    content: " · stale";
    color: #c00;
    font-size: 11px;
    font-weight: 700;
  }
  @media (max-width: 800px) {
    .glance, .more-grid { grid-template-columns: 1fr 1fr; }
    .q-name { font-size: 13px; }
    .q-price { font-size: 17px; }
    .q-move, .q-fut, .q-asof, .markets-kicker, .markets-src { font-size: 13px; }
    .markets-outage { font-size: 16px; }
  }
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)
