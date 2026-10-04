# The Flying High Club — website

Live site: https://www.theflyinghighclub.com  ·  Hosted on Cloudflare Pages (deploys automatically on every change to this repository).

## What updates itself
Every 6 hours a GitHub Action (`.github/workflows/update-site.yml`) runs `scripts/update_site.py`, which:
- reads the podcast RSS feed and the YouTube channel feed
- adds any new episode to the site: hero, latest-episode card, roster, search, episode guide, Google/AI structured data, `llms.txt`
- links each episode to its YouTube video when it finds one, otherwise Spotify
- downloads episode artwork (RSS art, or the YouTube thumbnail) into `site/guests/auto/`
- pulls live YouTube views + subscribers if the `YOUTUBE_API_KEY` secret is set

Run it now: **Actions → Update site from podcast feed → Run workflow**.

## Guest photos and company logos (automatic)
1. **Logo:** upload the company's logo to `site/logos/`, named after the company: `kenya-airways.png`, `swissport.png`, `iata.png`. Transparent PNG works best. Empty space around it is trimmed automatically.
2. **Photo:** upload the guest's photo to `photos/`, named after the guest: `george-kamal.jpg`.

That's it. The **Style guest photos** action cuts the person out, makes them black & white on the FHC navy, adds their company logo top-right, attaches the photo to their episode and refreshes the site. The logo also appears in the "Guests have joined us from" carousel automatically.

How the right logo is chosen, in order:
- **File-name override:** `person--logo.jpg`, e.g. `sam-chui--emirates.jpg` uses `site/logos/emirates.png`; `warwick-brady--none.jpg` = no logo.
- **`"logo"` in data/episodes.json** for that episode, if you've set one.
- **Automatic:** the logo whose name appears in the guest's company (or job title). Small words like UK / Group / Ltd are ignored, so `microsoft.png` matches "Microsoft UK". In two-guest episodes each person gets their own company.

Adding a logo later also updates the photos already on the site. After each run, open the action to see a table of who got which logo and why, including anything it couldn't match.

## What you edit by hand
| To change… | Edit |
|---|---|
| A guest's name, role, company, quote, summary, search topics | `data/episodes.json` |
| Audience stats, cockpit numbers, links, form email, map cities, carousel company list | the `SITE CONFIG` block near the bottom of `site/index.html` |

New episodes are added to `data/episodes.json` with `"auto": true` and a best-guess guest name and role taken from the title. Check them, fix anything, and delete the `"auto": true` line once you're happy. Your edits are never overwritten.

**Don't edit** anything between `AUTO:…:START` and `AUTO:…:END` markers in `index.html`; the scripts rewrite those.

## Curating the site
- **Featured guests:** add `"featured": true,` to an episode in `data/episodes.json` to show it in the Roster cards (the 3 newest episodes always show).
- **Hide an episode:** add `"hidden": true,` to its entry — it disappears from the site and is never re-added.
- **Logo strip:** ranked list `guestCompanies` in `SITE CONFIG` (`site/index.html`); only companies with a logo file in `site/logos/` are shown, up to 16.
- **YouTube-only episodes:** full-length videos (15 min+) since September 2023 are added automatically; any you add by hand always show.
