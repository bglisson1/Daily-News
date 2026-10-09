"""Tests for market cards: parsing, a failed source, a total outage, and the stale banner."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import build
import markets
from markets import EASTERN

ROOT = Path(__file__).resolve().parents[1]


def et(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN)


def stamp(when: datetime) -> int:
    return int(when.timestamp())


def yahoo_payload(symbol: str, price: float, market_time: datetime, bars: list[tuple[datetime, float]]) -> bytes:
    body = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "regularMarketPrice": price,
                        "regularMarketTime": stamp(market_time),
                    },
                    "timestamp": [stamp(when) for when, _close in bars],
                    "indicators": {"quote": [{"close": [close for _when, close in bars]}]},
                }
            ],
            "error": None,
        }
    }
    return json.dumps(body).encode()


TREASURY_CSV = """Date,"1 Mo","2 Yr","10 Yr"
10/08/2026,4.14,4.75,5.22
10/07/2026,4.07,4.77,5.28
"""

FREDDIE_CSV = """date,pmms30,pmms30p
10/1/2026,7.30,
10/8/2026,7.40,
"""

FRED_DGS10 = """observation_date,DGS10
2026-10-07,5.28
2026-10-08,.
2026-10-06,5.20
2026-10-08,5.22
"""

FRED_MORTGAGE = """observation_date,MORTGAGE30US
2026-10-01,7.30
2026-10-08,7.40
"""


def sample_config() -> dict:
    return {
        "markets": {
            "enabled": True,
            "at_a_glance": [
                {"label": "S&P 500", "symbol": "^GSPC", "kind": "close", "futures": "ES=F"},
                {"label": "Dow", "symbol": "^DJI", "kind": "close"},
                {"label": "30-yr mortgage", "series": "MORTGAGE30US", "kind": "mortgage"},
            ],
            "more": [
                {"label": "10-year", "series": "DGS10", "tenor": "10 Yr", "kind": "treasury"},
                {"label": "S&P fut", "symbol": "ES=F", "kind": "futures"},
                {"label": "Bitcoin", "symbol": "BTC-USD", "kind": "live", "prefix": "$", "decimals": 0},
            ],
        }
    }


def sample_bars():
    return [
        (et(2026, 10, 6, 9, 30), 90.0),
        (et(2026, 10, 7, 9, 30), 100.0),
        (et(2026, 10, 8, 9, 30), 110.0),
    ]


def router(now, *, fail=(), old=False):
    """Fake network. `fail` is a set of source names: yahoo symbols, treasury, freddie, fred."""

    def getter(url, timeout, user_agent, accept="*/*"):
        if "finance.yahoo.com" in url:
            for symbol in ("^GSPC", "^DJI", "ES=F", "BTC-USD"):
                encoded = symbol.replace("^", "%5E")
                if encoded not in url and symbol not in url:
                    continue
                if symbol in fail or "yahoo" in fail:
                    raise RuntimeError(f"HTTP 500 for {symbol}")
                if old:
                    ancient = [
                        (et(2026, 8, 1, 9, 30), 10.0),
                        (et(2026, 8, 2, 9, 30), 4242.42),
                    ]
                    return yahoo_payload(symbol, 4242.42, et(2026, 8, 2, 16, 0), ancient)
                price = {"^GSPC": 110.0, "^DJI": 200.0, "ES=F": 111.5, "BTC-USD": 50000.0}[symbol]
                market_time = et(2026, 10, 8, 16, 46) if symbol != "BTC-USD" else et(2026, 10, 8, 16, 50)
                return yahoo_payload(symbol, price, market_time, sample_bars())
            raise RuntimeError(f"unexpected yahoo url {url}")
        if "daily-treasury-rates.csv" in url:
            if "treasury" in fail:
                raise RuntimeError("Treasury timed out")
            return TREASURY_CSV.encode()
        if "PMMS_history.csv" in url:
            if "freddie" in fail:
                raise RuntimeError("Freddie Mac timed out")
            return FREDDIE_CSV.encode()
        if "fredgraph.csv" in url:
            if "fred" in fail:
                raise RuntimeError("FRED timed out")
            if "MORTGAGE30US" in url:
                return FRED_MORTGAGE.encode()
            return FRED_DGS10.encode()
        raise RuntimeError(f"unexpected url {url}")

    return getter


def card(html: str, symbol: str) -> str:
    marker = f'data-symbol="{symbol}"'
    assert marker in html, html
    return html.split(marker, 1)[1].split("</article>", 1)[0]


def test_parse_yahoo_previous_close_after_the_bell():
    result = json.loads(yahoo_payload("^GSPC", 110.0, et(2026, 10, 8, 16, 46), sample_bars()))
    bars = markets.daily_bars(result["chart"]["result"][0])
    level, change, pct, as_of = markets.cash_close(bars, et(2026, 10, 8, 17, 0))
    assert level == pytest.approx(110.0)
    assert change == pytest.approx(10.0)
    assert pct == pytest.approx(10.0)
    assert as_of.date().isoformat() == "2026-10-08"


def test_parse_yahoo_previous_close_drops_the_open_session():
    result = json.loads(yahoo_payload("^GSPC", 112.0, et(2026, 10, 8, 11, 0), sample_bars()))
    bars = markets.daily_bars(result["chart"]["result"][0])
    level, change, pct, as_of = markets.cash_close(bars, et(2026, 10, 8, 11, 0))
    assert level == pytest.approx(100.0)
    assert change == pytest.approx(10.0)
    assert as_of.date().isoformat() == "2026-10-07"


def test_parse_live_print_labels_a_fresh_price_live():
    bars = sample_bars()
    price, change, pct, as_of, fresh = markets.live_print(
        bars, 112.0, et(2026, 10, 8, 16, 50), et(2026, 10, 8, 17, 0)
    )
    assert fresh is True
    assert price == pytest.approx(112.0)
    assert change == pytest.approx(12.0)
    assert as_of.hour == 16


def test_parse_treasury_csv_and_basis_points():
    rows = []
    import csv
    import io

    for row in csv.DictReader(io.StringIO(TREASURY_CSV)):
        rows.append(row)
    # Newest row is first in the file. Parsing sorts and keeps one row per day.
    rows = rows + rows
    points = markets.treasury_series(rows, "10 Yr")
    assert [point[0].date().isoformat() for point in points] == ["2026-10-07", "2026-10-08"]
    assert points[-1][1] == pytest.approx(5.22)
    assert points[-2][1] == pytest.approx(5.28)


def test_parse_freddie_weekly_file():
    points = markets.parse_freddie_csv(FREDDIE_CSV)
    assert points[-1][0].date().isoformat() == "2026-10-08"
    assert points[-1][1] == pytest.approx(7.4)
    assert points[-2][1] == pytest.approx(7.3)


def test_parse_fred_csv_skips_missing_values():
    points = markets.parse_two_column_csv(FRED_DGS10)
    assert [point[0].date().isoformat() for point in points] == [
        "2026-10-06",
        "2026-10-07",
        "2026-10-08",
    ]
    assert points[-1][1] == pytest.approx(5.22)


def test_one_source_fails_and_the_others_still_show_numbers():
    now = et(2026, 10, 8, 17, 0)
    report = markets.fetch_markets(sample_config(), now, router(now, fail={"^DJI"}))
    assert report.outage is False
    dow = next(quote for quote in report.glance if quote.symbol == "^DJI")
    spx = next(quote for quote in report.glance if quote.symbol == "^GSPC")
    mortgage = next(quote for quote in report.glance if quote.symbol == "MORTGAGE30US")
    assert dow.ok is False
    assert dow.price is None
    assert spx.ok is True
    assert spx.price == pytest.approx(110.0)
    assert spx.session_label == "Previous close"
    assert mortgage.ok is True
    assert mortgage.price == pytest.approx(7.4)
    assert mortgage.session_label == "Weekly"
    assert mortgage.source_label == "Freddie Mac"
    html = markets.render_markets(report)
    assert "unavailable" in card(html, "^DJI")
    assert "200.00" in card(html, "^GSPC") or "110.00" in card(html, "^GSPC")
    assert "110.00" in card(html, "^GSPC")
    assert "Previous close" in card(html, "^GSPC")
    assert "7.40%" in card(html, "MORTGAGE30US")
    assert "Weekly" in card(html, "MORTGAGE30US")
    assert "Market data is unavailable" not in html


def test_treasury_uses_fred_when_the_official_file_fails():
    now = et(2026, 10, 8, 17, 0)
    report = markets.fetch_markets(sample_config(), now, router(now, fail={"treasury"}))
    ten = next(quote for quote in report.more if quote.symbol == "DGS10")
    assert ten.ok is True
    assert ten.source_label == "FRED"
    assert ten.price == pytest.approx(5.22)
    assert ten.bp == -6
    html = markets.render_markets(report)
    assert "FRED" in card(html, "DGS10")
    assert "Previous close" in card(html, "DGS10")
    assert "FRED filled in" in html


def test_all_sources_fail_shows_unavailable_and_no_numbers():
    now = et(2026, 10, 8, 17, 0)
    report = markets.fetch_markets(
        sample_config(),
        now,
        router(now, fail={"yahoo", "treasury", "freddie", "fred"}),
    )
    assert report.outage is True
    assert report.all_quotes()
    assert all(not quote.ok and quote.price is None for quote in report.all_quotes())
    html = markets.render_markets(report)
    assert "Market data is unavailable" in html
    for quote in report.all_quotes():
        body = card(html, quote.symbol)
        assert "unavailable" in body
        assert "110.00" not in body
        assert "7.40" not in body
        assert "5.22" not in body
        assert "50,000" not in body


def test_old_source_data_is_unavailable_instead_of_shown():
    now = et(2026, 10, 8, 17, 0)
    report = markets.fetch_markets(sample_config(), now, router(now, old=True, fail={"treasury", "freddie", "fred"}))
    spx = next(quote for quote in report.glance if quote.symbol == "^GSPC")
    assert spx.ok is False
    assert spx.price is None
    html = markets.render_markets(report)
    assert "4242" not in html
    assert "unavailable" in card(html, "^GSPC")


def test_disabled_markets_render_nothing():
    config = {"markets": {"enabled": False}}
    report = markets.fetch_markets(config, et(2026, 10, 8, 17, 0), router(et(2026, 10, 8, 17, 0)))
    assert report.enabled is False
    assert markets.render_markets(report) == ""


def test_broken_market_config_stops_the_build(tmp_path: Path):
    with pytest.raises(SystemExit, match="missing the markets:"):
        markets.validate_markets({"site": {}, "sections": []})

    broken = {
        "markets": {
            "enabled": True,
            "at_a_glance": [{"label": "S&P 500", "kind": "close"}],
            "more": [{"label": "10-year", "series": "DGS10", "tenor": "10 Yr", "kind": "treasury"}],
        }
    }
    with pytest.raises(SystemExit, match="needs a symbol"):
        markets.validate_markets(broken)

    path = tmp_path / "feeds.yml"
    path.write_text(
        "site:\n  title: Test\nsections:\n  - name: Politics\n    sources: []\n"
        "markets:\n  enabled: yes\n  at_a_glance:\n    - label: S&P 500\n      kind: nope\n"
        "  more:\n    - label: Oil\n      symbol: CL=F\n      kind: live\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="kind:"):
        build.load_config(path)


def test_repo_feeds_file_passes_validation():
    config = build.load_config(ROOT / "feeds.yml")
    assert config["markets"]["at_a_glance"]
    assert config["markets"]["more"]
    symbols = [row.get("symbol") for row in config["markets"]["at_a_glance"]]
    assert "^GSPC" in symbols
    odd = [
        source
        for section in config["sections"]
        if section["name"] == "Tech & Odd"
        for source in section["sources"]
    ]
    urls = [source["url"] for source in odd]
    assert "https://rss.upi.com/news/odd_news.rss" in urls
    assert all("weird-but-true" not in url for url in urls)


def test_stale_banner_weekday_daytime_limit_is_eight_hours():
    now = et(2026, 10, 9, 14, 0)  # Friday 2:00 PM
    assert markets.stale_limit_hours(now) == 8
    assert markets.page_is_stale(et(2026, 10, 9, 13, 0), now) is False
    assert markets.page_is_stale(et(2026, 10, 9, 6, 0), now) is False  # exactly 8 hours
    assert markets.page_is_stale(et(2026, 10, 9, 5, 59), now) is True
    assert markets.page_is_stale(et(2026, 10, 9, 5, 0), now) is True


def test_stale_banner_overnight_and_weekend_allow_eighteen_hours():
    friday_night = et(2026, 10, 9, 22, 0)
    assert markets.stale_limit_hours(friday_night) == 18
    assert markets.page_is_stale(et(2026, 10, 9, 16, 30), friday_night) is False

    saturday = et(2026, 10, 10, 10, 0)
    assert markets.stale_limit_hours(saturday) == 18
    friday_close_build = et(2026, 10, 9, 16, 30)
    assert markets.page_is_stale(friday_close_build, saturday) is False  # 17.5 hours
    assert markets.page_is_stale(friday_close_build, et(2026, 10, 10, 11, 0)) is True  # 18.5 hours

    # Monday before 8:00 AM is still the overnight rule.
    monday_early = et(2026, 10, 12, 7, 30)
    sunday_build = et(2026, 10, 11, 16, 30)
    assert markets.stale_limit_hours(monday_early) == 18
    assert markets.page_is_stale(sunday_build, monday_early) is False
    # At 8:30 AM the weekday rule is 8 hours, so Sunday's build is stale.
    monday_morning = et(2026, 10, 12, 8, 30)
    assert markets.stale_limit_hours(monday_morning) == 8
    assert markets.page_is_stale(sunday_build, monday_morning) is True
    assert markets.page_is_stale(et(2026, 10, 12, 6, 10), monday_morning) is False


def test_stale_banner_uses_eastern_time_in_winter():
    now = et(2027, 1, 15, 14, 0)  # Thursday, Eastern Standard Time
    assert markets.stale_limit_hours(now) == 8
    assert markets.page_is_stale(et(2027, 1, 15, 5, 0), now) is True
    assert now.utcoffset() == timedelta(hours=-5)


def test_stale_banner_text_and_page_shell():
    built = et(2026, 10, 8, 16, 44)
    assert markets.stale_banner_text(built) == "This page is out of date: last updated Oct 8, 4:44 PM ET"
    page = build.render_page(
        site={"title": "BLAKE'S DAILY NEWS", "max_age_hours": 36},
        updated=built,
        banner=None,
        splash=[],
        sections={},
        section_order=[],
        market_html='<section id="markets">MARK</section>',
    )
    assert "This page is out of date: last updated Oct 8, 4:44 PM ET" in page
    assert 'id="stale-banner"' in page
    assert "hidden" in page
    assert "MARK" in page
    payload = json.loads(page.split('id="freshness-data"', 1)[1].split(">", 1)[1].split("</script>", 1)[0])
    assert payload["dayHours"] == markets.DAY_LIMIT_HOURS
    assert payload["offHours"] == markets.OFF_LIMIT_HOURS
    assert payload["dayStart"] == markets.DAY_START_HOUR
    assert payload["dayEnd"] == markets.DAY_END_HOUR
    assert payload["built"].startswith("2026-10-08T16:44:00")
