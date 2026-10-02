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

## What you edit by hand
| To change… | Edit |
|---|---|
| A guest's name, role, company, quote, summary, search topics | `data/episodes.json` |
| A guest headshot | upload to `site/guests/`, then set `"photo": "guests/file.jpg"` in `data/episodes.json` (optional `"photoPosition": "center 20%"` to adjust the crop) |
| Audience stats, cockpit numbers, links, form email, map cities, guest-company logos | the `SITE CONFIG` block near the bottom of `site/index.html` |
| Company logos | upload to `site/logos/` and set `logo:` in `guestCompanies` |

New episodes are added to `data/episodes.json` with `"auto": true` and a best-guess guest name and role taken from the title. Check them, fix anything, and delete the `"auto": true` line once you're happy. Your edits are never overwritten.

**Don't edit** anything between `AUTO:…:START` and `AUTO:…:END` markers in `index.html`; the script rewrites those.
