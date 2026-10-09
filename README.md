# Blake's Daily News

A simple news page for Blake, in the style of the old Drudge Report: a white page, a big top headline, and columns of links. No ads. No accounts. Each link opens the original story in a new tab.

The page is rebuilt on its own from free public news feeds. You change what it shows by editing one file, `feeds.yml`. You do not need to write code.

**The site:** https://bglisson1.github.io/Daily-News/

It updates three times on weekdays and weekends:

- about 6:00 AM Eastern
- about 11:30 AM Eastern
- about 4:30 PM Eastern

GitHub sometimes skips or delays a scheduled run. A second run follows about an hour and fifteen minutes later (about 7:15 AM, 12:45 PM, and 5:45 PM Eastern during daylight time). If the first run already worked, the second one just refreshes the page. If the first one was skipped, the second one is the backup.

During daylight time those clocks are exact. In winter (Eastern Standard Time) they land one hour earlier. It also updates whenever a change is saved on the `main` branch, and whenever you run it by hand.

If the page in your browser is older than it should be, a red banner at the top says so: "This page is out of date: last updated" and the time. On a weekday between 8:00 AM and 9:00 PM Eastern, that happens when the last update is more than 8 hours old. Overnight, and all day Saturday and Sunday, the limit is 18 hours, because the gap from 4:30 PM to the next morning is about 13 hours and a late run needs a little room. The market numbers stay on the page, but they are marked stale so they are not mistaken for a fresh quote. That check runs in the browser. It needs JavaScript, which is already on for a normal visit.

GitHub turns off scheduled updates on a public project after 60 days with no new commit. Each run turns that schedule back on with the token GitHub already gives the Action. There is no password to add. If the project has gone 45 days without a commit, the run also saves a date in a file named `.keepalive`. That file only exists so the schedule stays on. You can ignore it.

Headlines older than 36 hours are left off. If the same story shows up in several places, it is shown once. The big headline is the story that showed up in the most places, with an extra lift for words like Tampa, Florida, the Bucs, and markets.

A spinning red siren appears above that headline only for a truly huge story (an assassination, a president elected, a war declared, a major terror attack, a sitting president resigning or being impeached, and events like those). On the normal setting, `siren_override: auto`, the top story has to contain a phrase from `siren_keywords` and be carried by at least `siren_min_sources` different outlets (8) in the last `siren_max_age_hours` (6). Set `siren_override` to `on` to force the light, optionally with `siren_headline` and `siren_url`. Set it to `off` to keep the light dark. Leave it on `auto`.

Market cards sit under the title, above the big headline. At a glance: S&P 500, Dow, Russell 2000, and the Freddie Mac 30-year mortgage. More markets: Nasdaq, the 10-year and 2-year Treasury yields, stock futures, oil, gold, VIX, and Bitcoin. Each card says what kind of number it is (previous close, live, premarket, or weekly) and the date. If a source does not answer, that card says **unavailable**. The page does not keep yesterday's number and pretend it is new. If every market source fails, a line on the page says the market data is unavailable, and the news columns still publish.

## The one setup step

GitHub has to be told to publish the page. Do this once:

1. Open [github.com/bglisson1/Daily-News](https://github.com/bglisson1/Daily-News).
2. Click **Settings** in the top menu of the repository.
3. In the left sidebar, click **Pages**.
4. Under **Build and deployment**, find **Source**.
5. Click the dropdown and choose **GitHub Actions**.

You do not pick a branch. Leave Source set to GitHub Actions. After the next successful run, the site address above will show the page. The first run can take a couple of minutes.

## Run an update by hand

1. Open the repository on GitHub.
2. Click the **Actions** tab.
3. In the left sidebar, click **Build daily news**.
4. Click the **Run workflow** button on the right.
5. Leave the branch set to **main**.
6. Click the green **Run workflow** button.

Refresh the site in a minute or two. Open a run with a red X and read the error. If the job named **Deploy to GitHub Pages** is green, the page itself did update. A single news site being down does not cause a red X. One market quote failing does not either. Every market source failing does not either: the page says the numbers are unavailable. A typo in `feeds.yml` does cause a red X. So does a failing test. The tests run before the page is published.

## Change sources or keywords

Everything you would want to change is in `feeds.yml`. The notes at the top of that file walk through each kind of edit. Short version:

1. Open the repository on GitHub.
2. Click the file named **feeds.yml**.
3. Click the pencil icon (**Edit this file**) at the upper right of the file.
4. Make the change.
   - To add a source, copy one of the blocks that starts with `- name:` and paste it under the section where it belongs. Change the name, paste the feed address into `url`, and set `max_items` (how many headlines to keep from that source).
   - To turn a source off, change `enabled: yes` to `enabled: no` under that source.
   - To block a kind of headline, add a line under `block_keywords`.
   - To float a topic higher, add a line under `boost_keywords`.
   - To move a section left, center, or right, change its `column` number (`1`, `2`, or `3`).
   - To change a market card, edit the `markets:` block. Change the `label` to rename it, or the `symbol` to point it at a different ticker. Set `enabled: no` under one card to hide it. Set `enabled: no` under `markets:` to hide the whole box. Do not delete the `markets:` heading.
5. Click **Commit changes...** (green button).
6. Leave **Commit directly to the `main` branch** selected.
7. Click **Commit changes**.

The page rebuilds from that save. Keep the quotes around web addresses. Keep the indentation lined up with the block you copied. If a line is indented wrong, the update fails and the error in the Actions tab names the problem.

A feed address is the site's RSS or Atom link. It usually ends in `.xml`, `/feed/`, or `/rss`. Paste the whole address, including `https://`.

## What is on the page

Market cards first, then six news sections:

| Section | What it is |
| --- | --- |
| Markets | Under the title. S&P 500, Dow, Russell 2000, and the 30-year mortgage, then Nasdaq, 10-year and 2-year Treasuries, futures, oil, gold, VIX, and Bitcoin. |
| Politics | Left column, first on a phone. Fox News politics, New York Post politics, Washington Examiner, Daily Caller, Townhall, Breitbart, The Federalist, National Review, Daily Wire, Just the News, RealClearPolitics, The Hill, Axios, Politico. |
| National | Fox News US, BBC World, AP, Reuters. Sports and entertainment links are left out. |
| Business & Markets | CNBC, MarketWatch, Bloomberg Markets, Yahoo Finance, Seeking Alpha |
| Tampa Bay & Florida | Tampa Bay Times, Fox 13 Tampa Bay, WFLA, Florida Politics |
| Sports | Tampa Bay only: Buccaneers, Pewter Report, USF Bulls football, Rays, Lightning, and Tampa Bay Times sports. A Times story has to name a Tampa Bay team. |
| Tech & Odd | New York Post tech, Fox News tech, The Verge, UPI odd news |

On a phone the three columns stack into one column.

## Feeds that were swapped

These were checked live. Every source in `feeds.yml` returned stories. A few of the obvious addresses were dead or frozen, so a working address is used instead:

| Wanted | What happened | What the page uses |
| --- | --- | --- |
| Wall Street Journal | The free feeds at `feeds.a.dj.com` only contained stories from January 2025, so nothing from the last 36 hours. | Bloomberg Markets and Seeking Alpha, plus CNBC, MarketWatch, and Yahoo Finance. |
| Yahoo Finance news | `finance.yahoo.com/news/rssindex` was stuck on stories from September 23, 2026. | Yahoo Finance's live market-headline feed. |
| AP News | `apnews.com` no longer offers a public RSS feed (it answers "forbidden"). | A Google News feed limited to stories on apnews.com. The link opens the AP story. |
| Reuters | The old Reuters RSS addresses are gone. | A Google News feed limited to stories on reuters.com. |
| ESPN, CBS Sports, Yahoo Sports | National sports feeds are not used. | The sports column is Tampa Bay teams only. |
| Florida Politics | `floridapolitics.com/feed/` answers "forbidden" to an automatic download. | A Google News feed limited to floridapolitics.com. |
| Tampa Bay Times | `tampabay.com/feed/` does not exist. | The Times' working news feed and sports feed (their Arc RSS addresses). |
| Fox 13 Tampa Bay | `fox13news.com/feed` and `/rss` do not exist. | The station's own local-news feed, `fox13news.com/rss/category/local-news`. |
| Daily Wire | `dailywire.com/rss.xml` is only a redirect page. | `dailywire.com/feeds/rss.xml`. |
| USF Bulls football | `gousfbulls.com/rss.aspx` came back empty. | `gousfbulls.com/rss?path=football`. |
| Odd news | UPI's old odd-news address answered "forbidden," and Oddity Central's newest item was from September 18, 2026. New York Post's Weird But True feed then went quiet: its newest story was October 3, 2026, outside the 36-hour window. | UPI's current odd-news feed, `rss.upi.com/news/odd_news.rss`. |
| Newsmax | The feed timed out, so it is not included. | The Federalist feed does work and is in Politics, though it is often only one fresh story. |
| Politico | `politico.com` RSS answers "forbidden." | A Google News feed limited to politico.com. |
| Axios politics | Axios does not publish a politics-only feed. | A Google News feed of politics stories on axios.com. |
| Tampa Bay Lightning | The NHL Lightning feed came back empty. | Raw Charge, a Lightning news site. |
| Tampa Bay Rays | The team feed at mlb.com/rays works. | Used as-is. |

Pewter Report and Tampa Bay Times sports are extra Bucs and local sports sources. Both returned fresh stories.
