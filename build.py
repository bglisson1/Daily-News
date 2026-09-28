#!/usr/bin/env python3
"""Build Blake's Daily News.

You do not need to edit this file. Change feeds.yml instead, then run:

    python build.py

The script reads every source in feeds.yml, drops old and blocked headlines,
skips near-duplicate headlines, and writes index.html next to this file.
If one news site is down, that source is skipped and the rest of the page
is still built.
"""

from __future__ import annotations

import html
import re
import sys
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "feeds.yml"
OUTPUT_PATH = ROOT / "index.html"
EASTERN = ZoneInfo("America/New_York")
USER_AGENT = (
    "Mozilla/5.0 (compatible; BlakesDailyNews/1.0; "
    "+https://bglisson1.github.io/Daily-News/)"
)
FETCH_TIMEOUT_SECONDS = 20
MAX_FEED_BYTES = 2_000_000

# Glue words that should not count when deciding two headlines are the same story.
STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on",
    "or", "the", "to", "with", "after", "over", "its", "it", "his", "her",
    "their", "them", "they", "she", "him", "you", "your", "our", "this", "that",
    "have", "has", "had", "was", "were", "are", "been", "be", "will", "would",
    "could", "should", "about", "amid", "during", "while", "before", "under",
    "again", "now", "still", "also", "only", "back", "off", "than", "then",
    "but", "not", "who", "how", "why", "all", "out", "just", "more", "most",
    "when", "what", "says", "said", "new", "news", "week", "game", "games",
    "report", "reports", "latest", "updates", "update", "live", "into", "from",
}


@dataclass
class Item:
    title: str
    link: str
    source: str
    section: str
    column: int
    published: datetime
    image: str | None = None
    boosts: list[str] = field(default_factory=list)
    cluster_id: int = -1


@dataclass
class FeedReport:
    name: str
    url: str
    kept: int = 0
    error: str | None = None


def main() -> int:
    config = load_config(CONFIG_PATH)
    site = config["site"]
    max_age = timedelta(hours=float(site.get("max_age_hours", 36)))
    now = datetime.now(timezone.utc)

    reports: list[FeedReport] = []
    items: list[Item] = []
    for section in config["sections"]:
        if not as_bool(section.get("enabled", True)):
            continue
        section_name = str(section["name"]).strip()
        column = int(section.get("column", 1))
        for source in section.get("sources") or []:
            if not as_bool(source.get("enabled", True)):
                continue
            report, batch = fetch_source(
                source=source,
                section_name=section_name,
                column=column,
                block_keywords=config.get("block_keywords") or [],
                boost_keywords=config.get("boost_keywords") or [],
                skip_url_parts=section.get("skip_url_parts") or [],
                skip_keywords=section.get("skip_keywords") or [],
                require_keywords=section.get("require_keywords") or [],
                require_match=source_requires_match(section, source),
                max_age=max_age,
                now=now,
            )
            reports.append(report)
            items.extend(batch)

    clusters = cluster_items(items)
    banner, splash, sections = arrange(items, clusters, site)

    updated = datetime.now(EASTERN)
    page = render_page(
        site=site,
        updated=updated,
        banner=banner,
        splash=splash,
        sections=sections,
        section_order=config["sections"],
    )
    OUTPUT_PATH.write_text(page, encoding="utf-8")

    shown = (1 if banner else 0) + len(splash)
    shown += sum(len(rows) for rows in sections.values())
    failed = [report for report in reports if report.error]
    print(f"Wrote {OUTPUT_PATH.name}: {shown} headlines.")
    for report in reports:
        if report.error:
            print(f"  SKIP {report.name}: {report.error}", file=sys.stderr)
        else:
            print(f"  OK   {report.name}: {report.kept} headlines")
    if failed:
        print(
            f"{len(failed)} feed(s) skipped. The page was still built.",
            file=sys.stderr,
        )
    if banner:
        outlets = sorted({item.source for item in items if item.cluster_id == banner.cluster_id})
        print(f"Top story ({len(outlets)} sources): {banner.title}")
    return 0


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            "Could not find feeds.yml next to build.py. "
            "That file is the list of news sources."
        )
    try:
        with path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise SystemExit(
            "feeds.yml could not be read. A line is probably indented wrong "
            f"or is missing a quote.\n{exc}"
        ) from exc
    if not isinstance(config, dict):
        raise SystemExit("feeds.yml needs a site: section and a sections: list.")
    if "site" not in config or "sections" not in config:
        raise SystemExit(
            "feeds.yml is missing site: or sections:. "
            "Put those headings back, or undo your last edit."
        )
    if not isinstance(config["sections"], list):
        raise SystemExit("sections: in feeds.yml must be a list of sections.")
    for section in config["sections"]:
        if not isinstance(section, dict) or not section.get("name"):
            raise SystemExit(
                "Every section in feeds.yml needs a name: line. "
                "Check the section you just edited."
            )
        for source in section.get("sources") or []:
            if not isinstance(source, dict) or not source.get("name"):
                raise SystemExit(
                    f"A source under {section['name']} is missing its name: line."
                )
            if as_bool(source.get("enabled", True)) and not source.get("url"):
                raise SystemExit(
                    f"The source '{source['name']}' is turned on but has no url. "
                    "Paste the feed address, or set enabled: no."
                )
    config.setdefault("block_keywords", [])
    config.setdefault("boost_keywords", [])
    return config


def fetch_source(
    source: dict,
    section_name: str,
    column: int,
    block_keywords: list,
    boost_keywords: list,
    skip_url_parts: list,
    skip_keywords: list,
    require_keywords: list,
    require_match: bool,
    max_age: timedelta,
    now: datetime,
) -> tuple[FeedReport, list[Item]]:
    name = str(source["name"]).strip()
    url = str(source["url"]).strip()
    report = FeedReport(name=name, url=url)
    try:
        max_items = int(source.get("max_items", 5))
    except (TypeError, ValueError):
        report.error = "max_items must be a whole number"
        return report, []
    if max_items < 1:
        return report, []

    try:
        entries = download_entries(url)
    except Exception as exc:  # a dead feed must not stop the build
        report.error = str(exc).strip() or exc.__class__.__name__
        return report, []

    strip_suffix = as_bool(source.get("strip_publisher_suffix", False))
    kept: list[Item] = []
    for entry in entries:
        try:
            item = entry_to_item(
                entry,
                source_name=name,
                section_name=section_name,
                column=column,
                strip_suffix=strip_suffix,
                block_keywords=block_keywords,
                boost_keywords=boost_keywords,
                skip_url_parts=skip_url_parts,
                skip_keywords=skip_keywords,
                require_keywords=require_keywords,
                require_match=require_match,
                max_age=max_age,
                now=now,
            )
        except Exception:
            # One bad story should not drop the rest of the feed.
            traceback.print_exc(file=sys.stderr)
            continue
        if item:
            kept.append(item)

    kept.sort(key=lambda item: item.published, reverse=True)
    kept = dedupe_links(kept)[:max_items]
    report.kept = len(kept)
    if not entries:
        report.error = "feed returned no stories"
    return report, kept


def download_entries(url: str) -> list:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = response.read(MAX_FEED_BYTES)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"could not connect ({reason})") from exc
    except TimeoutError as exc:
        raise RuntimeError("timed out") from exc

    parsed = feedparser.parse(payload)
    if parsed.entries:
        return list(parsed.entries)
    if getattr(parsed, "bozo", False):
        raise RuntimeError("feed was not valid RSS or Atom")
    return []


def entry_to_item(
    entry,
    source_name: str,
    section_name: str,
    column: int,
    strip_suffix: bool,
    block_keywords: list,
    boost_keywords: list,
    skip_url_parts: list,
    skip_keywords: list,
    require_keywords: list,
    require_match: bool,
    max_age: timedelta,
    now: datetime,
) -> Item | None:
    title = clean_text(entry.get("title") or "")
    link = clean_link(entry.get("link") or "")
    if not title or not link or not link.startswith(("http://", "https://")):
        return None
    if is_junk_title(title):
        return None

    published = entry_time(entry)
    if published is None or now - published > max_age or published - now > timedelta(hours=6):
        return None

    display_source = source_name
    if strip_suffix:
        title, publisher = split_publisher_suffix(title)
        if publisher:
            display_source = publisher
    # Google News titles look like "Mitch McConnell - Politico". The suffix
    # makes a topic-page label long enough to pass the first check.
    if not title or is_junk_title(title):
        return None

    if any(keyword_in(title.lower(), keyword) for keyword in block_keywords):
        return None
    if section_skips(title, link, entry, skip_url_parts, skip_keywords):
        return None
    if not passes_required(title, link, require_keywords, require_match):
        return None

    boosts = [str(keyword) for keyword in boost_keywords if keyword_in(title.lower(), keyword)]
    return Item(
        title=title,
        link=link,
        source=display_source,
        section=section_name,
        column=column,
        published=published,
        image=extract_image(entry),
        boosts=boosts,
    )


def source_requires_match(section: dict, source: dict) -> bool:
    """Mixed feeds can be limited to headlines that name a topic.

    A section's require_keywords list turns that on. A source can opt out
    with require_match: no when every story from that site already belongs,
    such as the Bucs' own feed.
    """
    keywords = section.get("require_keywords") or []
    if "require_match" in source:
        return as_bool(source.get("require_match"))
    return bool(keywords)


def passes_required(title: str, link: str, keywords: list, require_match: bool) -> bool:
    if not require_match or not keywords:
        return True
    path = urlsplit(link).path.lower().replace("-", " ")
    text = f"{title.lower()} {path}"
    return any(keyword_in(text, keyword) for keyword in keywords)


def section_skips(title: str, link: str, entry, skip_url_parts: list, skip_keywords: list) -> bool:
    """True when this section asked to leave the story out."""
    categories = entry_categories(entry)
    link_text = link.lower()
    for part in skip_url_parts:
        piece = str(part).strip().lower()
        if not piece:
            continue
        if piece in link_text:
            return True
        token = piece.strip("/")
        if token and any(token in category for category in categories):
            return True
    headline = title.lower()
    if any(keyword_in(headline, keyword) for keyword in skip_keywords):
        return True
    return False


def entry_categories(entry) -> list[str]:
    categories: list[str] = []
    for tag in entry.get("tags") or []:
        if isinstance(tag, dict):
            term = tag.get("term") or tag.get("label") or ""
        else:
            term = str(tag)
        if term:
            categories.append(str(term).lower())
    return categories


def entry_time(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def split_publisher_suffix(title: str) -> tuple[str, str | None]:
    if " - " not in title:
        return title, None
    headline, publisher = title.rsplit(" - ", 1)
    headline = headline.strip()
    publisher = publisher.strip()
    if headline and 2 <= len(publisher) <= 48:
        return headline, publisher
    return title, None


def extract_image(entry) -> str | None:
    candidates: list[tuple[int, str]] = []
    for media in entry.get("media_content") or []:
        url = media_url(media)
        if url and image_large_enough(media):
            candidates.append((pixel_area(media), url))
    for media in entry.get("media_thumbnail") or []:
        url = media_url(media)
        if url and image_large_enough(media):
            candidates.append((pixel_area(media), url))
    for enclosure in entry.get("enclosures") or []:
        url = enclosure.get("href") or enclosure.get("url")
        if url and looks_like_image(url, enclosure.get("type")):
            candidates.append((pixel_area(enclosure), absolute_image_url(url)))
    if candidates:
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]

    summary = entry.get("summary") or ""
    content = entry.get("content") or []
    if content and isinstance(content, list):
        summary = summary + " " + str(content[0].get("value") or "")
    match = re.search(r'<img[^>]+src=["\']([^"\']+)', summary, flags=re.I)
    if match and looks_like_image(match.group(1), None):
        return absolute_image_url(match.group(1))
    return None


def media_url(media: dict) -> str | None:
    url = media.get("url")
    if not url or not looks_like_image(url, media.get("type") or media.get("medium")):
        return None
    return absolute_image_url(url)


def looks_like_image(url: str, mime: str | None) -> bool:
    if mime and str(mime).lower().startswith("image/"):
        return True
    if mime and str(mime).lower() == "image":
        return True
    path = unquote(url).lower().split("?", 1)[0]
    return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def image_large_enough(media: dict) -> bool:
    for key in ("width", "height"):
        raw = media.get(key)
        if raw in (None, ""):
            continue
        try:
            if int(float(raw)) < 120:
                return False
        except (TypeError, ValueError):
            continue
    return True


def pixel_area(media: dict) -> int:
    try:
        return int(float(media.get("width") or 0)) * int(float(media.get("height") or 0))
    except (TypeError, ValueError):
        return 0


def absolute_image_url(url: str) -> str:
    url = unquote(url.strip())
    if url.startswith("//"):
        url = "https:" + url
    return url


def keyword_in(text: str, keyword) -> bool:
    phrase = str(keyword).strip().lower()
    if not phrase:
        return False
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


def clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


TRACKING_PARAMS = {
    "oc",
    "at_medium",
    "at_campaign",
    "ns_mchannel",
    "ns_source",
    "ns_campaign",
    "ns_linkname",
}


def clean_link(url: str) -> str:
    link = url.strip()
    # Google News RSS links bounce through an extra hop. The public article
    # address is the one a browser can open onto the original story.
    link = link.replace("https://news.google.com/rss/articles/", "https://news.google.com/articles/")
    link = link.replace("http://news.google.com/rss/articles/", "https://news.google.com/articles/")
    parts = urlsplit(link)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def canon_link(url: str) -> str:
    link = clean_link(url).rstrip("/")
    return link.lower()


def dedupe_links(items: list[Item]) -> list[Item]:
    seen: set[str] = set()
    unique: list[Item] = []
    for item in items:
        key = canon_link(item.link)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def is_junk_title(title: str) -> bool:
    """Skip tag pages, author indexes, and section labels that are not stories."""
    text = title.lower().strip()
    if len(text.split()) < 4:
        return True
    if re.search(r"\barchives?\b", text):
        return True
    if text.startswith("tag:"):
        return True
    if "latest news" in text or "latest and breaking" in text:
        return True
    return False


def normalize_title(title: str) -> str:
    text = title.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    words = []
    for word in text.split():
        if word in STOPWORDS:
            continue
        if word.isdigit():
            if len(word) >= 2:
                words.append(word)
            continue
        if len(word) <= 2:
            continue
        words.append(word)
    return " ".join(words)


def titles_match(left: str, right: str) -> bool:
    """True when two headlines are the same story, not merely the same topic."""
    if not left or not right:
        return False
    if left == right:
        return True
    left_words = set(left.split())
    right_words = set(right.split())
    if not left_words or not right_words:
        return False
    overlap = left_words & right_words
    if len(overlap) >= 4:
        return True
    union = left_words | right_words
    if len(overlap) >= 3 and len(overlap) / len(union) >= 0.55:
        return True
    shorter, longer = (
        (left_words, right_words)
        if len(left_words) <= len(right_words)
        else (right_words, left_words)
    )
    if len(shorter) >= 5 and len(shorter & longer) / len(shorter) >= 0.8:
        return True
    # Very close wording, such as a copied headline with a word added.
    if len(overlap) >= 3 and SequenceMatcher(None, left, right).ratio() >= 0.9:
        return True
    return False


def cluster_items(items: list[Item]) -> list[list[Item]]:
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    signatures = [normalize_title(item.title) for item in items]
    links = [canon_link(item.link) for item in items]
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            same_link = links[left] == links[right]
            if same_link or titles_match(signatures[left], signatures[right]):
                union(left, right)

    groups: dict[int, list[Item]] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)
    clusters: list[list[Item]] = []
    for group in groups.values():
        cluster_id = len(clusters)
        for item in group:
            item.cluster_id = cluster_id
        clusters.append(group)
    return clusters


def cluster_score(cluster: list[Item], now: datetime, max_age_hours: float) -> float:
    sources = {item.source.lower() for item in cluster}
    boosts: set[str] = set()
    for item in cluster:
        boosts.update(word.lower() for word in item.boosts)
    newest = max(item.published for item in cluster)
    age_hours = max(0.0, (now - newest).total_seconds() / 3600)
    freshness = max(0.0, 1 - (age_hours / max_age_hours)) if max_age_hours else 0
    # Source count dominates. Boost words are a smaller lift. Freshness
    # only breaks close calls.
    return len(sources) * 6 + len(boosts) * 2 + freshness * 3


def choose_representative(cluster: list[Item]) -> Item:
    def rank(item: Item) -> tuple:
        # Prefer a photo and a single clear headline over a semicolon roundup.
        length = len(item.title)
        roundup = item.title.count(";")
        readable = 1 if 40 <= length <= 120 else 0
        return (
            1 if item.image else 0,
            -roundup,
            len(item.boosts),
            readable,
            -abs(length - 85),
            item.published.timestamp(),
        )

    return max(cluster, key=rank)


def arrange(
    items: list[Item],
    clusters: list[list[Item]],
    site: dict,
) -> tuple[Item | None, list[Item], dict[str, list[Item]]]:
    if not items:
        return None, [], {}

    now = datetime.now(timezone.utc)
    max_age_hours = float(site.get("max_age_hours", 36))
    ranked = sorted(
        clusters,
        key=lambda cluster: cluster_score(cluster, now, max_age_hours),
        reverse=True,
    )
    banner = choose_representative(ranked[0])
    used = {banner.cluster_id}

    splash: list[Item] = []
    splash_count = int(site.get("splash_count", 8))
    for cluster in ranked[1:]:
        if len(splash) >= splash_count:
            break
        splash.append(choose_representative(cluster))
        used.add(cluster[0].cluster_id)

    by_section: dict[str, list[Item]] = {}
    seen_clusters: set[int] = set(used)
    leftovers = sorted(items, key=lambda item: (len(item.boosts), item.published), reverse=True)
    for item in leftovers:
        if item.cluster_id in seen_clusters:
            continue
        seen_clusters.add(item.cluster_id)
        by_section.setdefault(item.section, []).append(item)

    for rows in by_section.values():
        rows.sort(key=lambda item: (len(item.boosts), item.published), reverse=True)

    cap = int(site.get("max_headlines", 85))
    trim_to_cap(by_section, splash, banner, cap)
    return banner, splash, by_section


def trim_to_cap(
    by_section: dict[str, list[Item]],
    splash: list[Item],
    banner: Item | None,
    cap: int,
) -> None:
    def total() -> int:
        return (1 if banner else 0) + len(splash) + sum(len(rows) for rows in by_section.values())

    while total() > cap:
        # Drop the oldest unboosted link in the longest section. If every
        # remaining link is boosted, drop the oldest link in the longest section.
        candidates = [(name, rows) for name, rows in by_section.items() if len(rows) > 1]
        if not candidates:
            break
        name, rows = max(candidates, key=lambda pair: len(pair[1]))
        unboosted = [index for index, item in enumerate(rows) if not item.boosts]
        drop_at = unboosted[-1] if unboosted else len(rows) - 1
        del rows[drop_at]
        if not rows:
            del by_section[name]


def render_page(
    site: dict,
    updated: datetime,
    banner: Item | None,
    splash: list[Item],
    sections: dict[str, list[Item]],
    section_order: list,
) -> str:
    title = str(site.get("title") or "BLAKE'S DAILY NEWS")
    subtitle = str(site.get("subtitle") or "").strip()
    clock = updated.strftime("%I:%M %p").lstrip("0")
    red_splash = int(site.get("red_splash", 2))
    red_per_section = int(site.get("red_per_section", 2))
    max_age = site.get("max_age_hours", 36)

    columns: dict[int, list[tuple[str, list[Item]]]] = {1: [], 2: [], 3: []}
    for section in section_order:
        if not as_bool(section.get("enabled", True)):
            continue
        name = str(section["name"]).strip()
        rows = sections.get(name) or []
        if not rows:
            continue
        column = int(section.get("column", 1))
        if column not in columns:
            column = 1
        columns[column].append((name, rows))

    splash_html = []
    for index, item in enumerate(splash):
        css = "red" if index < red_splash else ""
        splash_html.append(
            "<li>"
            f'<a class="{css}" href="{esc(item.link)}" target="_blank" rel="noopener noreferrer">{esc(item.title)}</a> '
            f'<span class="src">({esc(item.source)})</span>'
            "</li>"
        )

    column_html = []
    for number in (1, 2, 3):
        blocks = []
        for name, rows in columns[number]:
            links = []
            for index, item in enumerate(rows):
                css = "red" if index < red_per_section else ""
                links.append(
                    "<li>"
                    f'<a class="{css}" href="{esc(item.link)}" target="_blank" rel="noopener noreferrer">{esc(item.title)}</a> '
                    f'<span class="src">({esc(item.source)})</span>'
                    "</li>"
                )
            blocks.append(
                '<section class="section">'
                f"<h2>{esc(name)}</h2>"
                f"<ul>{''.join(links)}</ul>"
                "</section>"
            )
        column_html.append(f'<div class="col">{"".join(blocks)}</div>')

    if banner:
        image_html = ""
        if banner.image and banner.image.startswith(("http://", "https://")):
            image_html = (
                f'<img src="{esc(banner.image)}" alt="{esc(banner.title)}" '
                'referrerpolicy="no-referrer" onerror="this.remove()">'
            )
        banner_html = (
            '<div class="banner">'
            f"{image_html}"
            f'<a class="lead" href="{esc(banner.link)}" target="_blank" rel="noopener noreferrer">{esc(banner.title)}</a>'
            f'<div class="lead-src">{esc(banner.source)}</div>'
            "</div>"
        )
    else:
        banner_html = (
            '<p class="empty">No headlines yet. Check feeds.yml, or wait for the next update.</p>'
        )

    subtitle_html = f'<p class="subtitle">{esc(subtitle)}</p>' if subtitle else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: #fff;
    color: #000;
    font-family: "Courier New", Courier, monospace;
    font-size: 16px;
    line-height: 1.35;
  }}
  a {{ color: #000; }}
  .wrap {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 16px 18px 48px;
  }}
  .masthead {{
    text-align: center;
    border-top: 4px solid #000;
    border-bottom: 4px solid #000;
    padding: 10px 8px 8px;
  }}
  h1 {{
    margin: 0;
    font-size: 40px;
    font-weight: 700;
    letter-spacing: 1px;
    line-height: 1.1;
  }}
  .subtitle {{
    margin: 4px 0 0;
    font-size: 14px;
    letter-spacing: 4px;
    text-transform: uppercase;
  }}
  .updated {{
    text-align: center;
    margin: 10px 0 6px;
    font-size: 14px;
  }}
  .banner {{
    text-align: center;
    margin: 8px auto 14px;
    max-width: 760px;
  }}
  .banner img {{
    display: block;
    max-width: min(640px, 100%);
    max-height: 360px;
    height: auto;
    margin: 0 auto 10px;
    object-fit: contain;
  }}
  a.lead {{
    color: #c00;
    font-size: 30px;
    font-weight: 700;
    line-height: 1.15;
    text-decoration: none;
    text-transform: uppercase;
  }}
  a.lead:hover {{ text-decoration: underline; }}
  .lead-src {{
    margin-top: 4px;
    font-size: 13px;
  }}
  .splash {{
    list-style: none;
    padding: 0;
    margin: 0 auto 18px;
    max-width: 680px;
    text-align: center;
  }}
  .splash li {{ margin: 5px 0; }}
  .splash a {{
    color: #000;
    text-decoration: none;
    font-size: 16px;
  }}
  .splash a.red {{ color: #c00; font-weight: 700; }}
  .splash a:hover, .section a:hover {{ text-decoration: underline; }}
  .columns {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 8px 28px;
    border-top: 1px solid #000;
    padding-top: 8px;
  }}
  .section {{ margin: 0 0 18px; }}
  .section h2 {{
    margin: 0 0 8px;
    padding: 4px 0;
    border-top: 3px solid #000;
    border-bottom: 1px solid #000;
    font-size: 15px;
    letter-spacing: 1px;
    text-align: center;
    text-transform: uppercase;
  }}
  .section ul {{
    list-style: none;
    margin: 0;
    padding: 0;
  }}
  .section li {{ margin: 0 0 8px; }}
  .section a {{
    color: #000;
    font-size: 15px;
    text-decoration: none;
  }}
  .section a.red, .splash a.red {{ color: #c00; font-weight: 700; }}
  .src {{
    color: #333;
    font-size: 12px;
    white-space: nowrap;
  }}
  .empty, .footer {{
    text-align: center;
    margin: 18px 0;
  }}
  .footer {{
    color: #333;
    font-size: 12px;
    border-top: 1px solid #000;
    padding-top: 10px;
  }}
  @media (max-width: 800px) {{
    .wrap {{ padding: 12px 14px 36px; }}
    h1 {{ font-size: 28px; }}
    a.lead {{ font-size: 22px; }}
    .columns {{ grid-template-columns: 1fr; }}
    .section a, .splash a {{ font-size: 17px; }}
    .src {{ font-size: 13px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <h1>{esc(title)}</h1>
    {subtitle_html}
  </header>
  <p class="updated">Updated {esc(clock)} ET</p>
  {banner_html}
  <ul class="splash">
    {''.join(splash_html)}
  </ul>
  <div class="columns">
    {''.join(column_html)}
  </div>
  <p class="footer">Headlines from the last {esc(max_age)} hours. Each link opens the original story.</p>
</div>
</body>
</html>
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
