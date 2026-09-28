# Blake's Daily News

A simple news page for Blake, in the style of the old Drudge Report: a white page, a big top headline, and columns of links. No ads. No accounts. Each link opens the original story in a new tab.

The page is rebuilt on its own from free public news feeds. You change what it shows by editing one file, `feeds.yml`. You do not need to write code.

**The site:** https://bglisson1.github.io/Daily-News/

It updates three times on weekdays and weekends:

- about 6:00 AM Eastern
- about 11:30 AM Eastern
- about 4:30 PM Eastern

During daylight time those are exact. In winter (Eastern Standard Time) they land one hour earlier. It also updates whenever a change is saved on the `main` branch, and whenever you run it by hand.

GitHub turns off scheduled updates on a public project after 60 days with no new commit. Each run turns that schedule back on with the token GitHub already gives the Action. There is no password to add. If the project has gone 45 days without a commit, the run also saves a date in a file named `.keepalive`. That file only exists so the schedule stays on. You can ignore it.

Headlines older than 36 hours are left off. If the same story shows up in several places, it is shown once. The big headline is the story that showed up in the most places, with an extra lift for words like Tampa, Florida, the Bucs, and markets.

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

Refresh the site in a minute or two. Open a run with a red X and read the error. If the job named **Deploy to GitHub Pages** is green, the page itself did update. A single news site being down does not cause a red X. A typo in `feeds.yml` does.

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
5. Click **Commit changes...** (green button).
6. Leave **Commit directly to the `main` branch** selected.
7. Click **Commit changes**.

The page rebuilds from that save. Keep the quotes around web addresses. Keep the indentation lined up with the block you copied. If a line is indented wrong, the update fails and the error in the Actions tab names the problem.

A feed address is the site's RSS or Atom link. It usually ends in `.xml`, `/feed/`, or `/rss`. Paste the whole address, including `https://`.

## What is on the page

Five sections:

| Section | What it is |
| --- | --- |
| National | Fox News, New York Post, Washington Examiner, National Review, Daily Wire, The Hill, Axios, BBC World, AP, Reuters |
| Business & Markets | CNBC, MarketWatch, Bloomberg Markets, Yahoo Finance, Seeking Alpha |
| Tampa Bay & Florida | Tampa Bay Times, Fox 13 Tampa Bay, WFLA, Florida Politics |
| Sports | Buccaneers, Pewter Report, USF Bulls football, CBS Sports, Yahoo Sports, Tampa Bay Times sports |
| Tech & Odd | New York Post tech, Fox News tech, The Verge, New York Post weird news |

On a phone the three columns stack into one column.

## Feeds that were swapped

These were checked live. Every source in `feeds.yml` returned stories. A few of the obvious addresses were dead or frozen, so a working address is used instead:

| Wanted | What happened | What the page uses |
| --- | --- | --- |
| Wall Street Journal | The free feeds at `feeds.a.dj.com` only contained stories from January 2025, so nothing from the last 36 hours. | Bloomberg Markets and Seeking Alpha, plus CNBC, MarketWatch, and Yahoo Finance. |
| Yahoo Finance news | `finance.yahoo.com/news/rssindex` was stuck on stories from September 23, 2026. | Yahoo Finance's live market-headline feed. |
| AP News | `apnews.com` no longer offers a public RSS feed (it answers "forbidden"). | A Google News feed limited to stories on apnews.com. The link opens the AP story. |
| Reuters | The old Reuters RSS addresses are gone. | A Google News feed limited to stories on reuters.com. |
| ESPN | `espn.com/espn/rss/nfl/news` came back empty. | CBS Sports for NFL news, and Yahoo Sports for general sports. |
| Florida Politics | `floridapolitics.com/feed/` answers "forbidden" to an automatic download. | A Google News feed limited to floridapolitics.com. |
| Tampa Bay Times | `tampabay.com/feed/` does not exist. | The Times' working news feed and sports feed (their Arc RSS addresses). |
| Fox 13 Tampa Bay | `fox13news.com/feed` and `/rss` do not exist. | The station's own local-news feed, `fox13news.com/rss/category/local-news`. |
| Daily Wire | `dailywire.com/rss.xml` is only a redirect page. | `dailywire.com/feeds/rss.xml`. |
| USF Bulls football | `gousfbulls.com/rss.aspx` came back empty. | `gousfbulls.com/rss?path=football`. |
| Odd news | UPI's odd-news feed answers "forbidden." Oddity Central's newest item was from September 18, 2026, outside the 36-hour window. | New York Post's Weird But True feed. |
| Newsmax, The Federalist | Newsmax timed out. The Federalist feed answers "forbidden." | Not included. National Review and the Daily Wire cover that part of the mix. |

Pewter Report and Tampa Bay Times sports are extra Bucs and local sports sources. Both returned fresh stories.
