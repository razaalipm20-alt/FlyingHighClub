#!/usr/bin/env python3
"""
The Flying High Club — automatic site updater.

Reads the podcast RSS feed (and the YouTube channel feed), then rewrites the
auto-managed parts of site/index.html, plus site/llms.txt and site/sitemap.xml.

Runs on a schedule via GitHub Actions (.github/workflows/update-site.yml).
Safe to run by hand too:   python scripts/update_site.py

What it does on each run
  • Every episode in the RSS feed appears on the site (roster, search, episode
    guide, latest-episode card, structured data for Google/AI).
  • New episodes are added to data/episodes.json with "auto": true so you can
    polish the guest name / role / quote later. Your edits are never overwritten.
  • Episode artwork (from the RSS feed, or the YouTube thumbnail) is downloaded
    to site/guests/auto/ and used until you add a proper headshot.
  • If the matching YouTube video is found, buttons point to YouTube.
  • If a YOUTUBE_API_KEY secret is set, live view + subscriber counts are pulled.

If a feed can't be reached, the script exits without touching anything, so a
temporary outage can never break the live site.
"""
import datetime as dt
import difflib
import email.utils
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ── Settings ────────────────────────────────────────────────────────────────
RSS_URL = "https://anchor.fm/s/e7dbe308/podcast/rss"
YT_CHANNEL_ID = "UCgMai80RXgjJHOhXpLUlKDg"
YT_FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"
SITE_URL = "https://www.theflyinghighclub.com/"
LINKS = {
    "youtube": "https://www.youtube.com/@TheFlyingHighClub",
    "spotify": "https://open.spotify.com/show/4cdbL0Sl4aMqkJ8XMLs4x9",
    "apple": "https://podcasts.apple.com/gb/podcast/the-flying-high-club-podcast/id1710279903",
    "amazon": "https://music.amazon.co.uk/podcasts/22adb086-d39c-4360-b107-bc3f7ba2bf0e",
    "linkedin": "https://www.linkedin.com/company/the-flying-high-club/",
    "instagram": "https://www.instagram.com/theflyinghighclubofficial/",
    "tiktok": "https://www.tiktok.com/@flyinghighclubofficial",
    "x": "https://x.com/TheFlyingHighC1",
    "rss": RSS_URL,
}
CONTACT_EMAIL = "raza.ali@theflyinghighclub.com"
SPOTIFY_SHOW_ID = "4cdbL0Sl4aMqkJ8XMLs4x9"

# YouTube videos at least this long count as full episodes (shorter = clips / Shorts).
FULL_EPISODE_MIN_MINUTES = 15
# Create episodes for full-length YouTube videos that aren't on the podcast feed yet.
YOUTUBE_FIRST_EPISODES = True
# Ignore YouTube videos published before this date when creating YouTube-only episodes.
# To remove a video that isn't a real episode, add  "hidden": true  to its entry in data/episodes.json
# — it disappears from the site and is never re-added.
YOUTUBE_ONLY_SINCE = "2023-09-01"

# Episode artwork (from the RSS feed, or the YouTube thumbnail) is always downloaded to
# site/guests/auto/ and handed to the photo styler, which turns clean headshots into
# branded photos with the company logo. USE_AUTO_ARTWORK = True would ALSO show the raw,
# unstyled artwork for guests the styler couldn't use (e.g. thumbnails full of text).
USE_AUTO_ARTWORK = False

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
INDEX = SITE / "index.html"
DATA = ROOT / "data" / "episodes.json"
AUTO_IMG_DIR = SITE / "guests" / "auto"

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
UA = {"User-Agent": "Mozilla/5.0 (FlyingHighClub site updater; +https://www.theflyinghighclub.com)"}

# Optional local test overrides (used for testing without internet)
RSS_FILE = os.environ.get("RSS_FILE")
YT_FILE = os.environ.get("YT_FILE")
NO_DOWNLOAD = os.environ.get("NO_DOWNLOAD") == "1"


def log(*a):
    print("[update]", *a, flush=True)


def fetch(url, binary=False, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


# ── Parsing ─────────────────────────────────────────────────────────────────
def strip_html(s):
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]+")


def auto_summary(desc, limit=230):
    """First meaningful sentence(s) of the episode description."""
    text = EMOJI.sub("", strip_html(desc))
    text = re.sub(r"https?://\S+", "", text)
    lines = [l.strip(" -•\t") for l in text.splitlines()]
    lines = [l for l in lines if len(l) > 40 and not re.match(r"(?i)(stay connected|follow|subscribe|check them out|join us|listen|host|guest)\b", l)]
    text = " ".join(lines)
    text = re.sub(r"(?i)^(welcome to [^.!]*[.!]\s*)", "", text)
    text = re.sub(r"(?i)^in this (special |inspiring )?episode of [^,]*,\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.split(r"(?i)\b(?:in this episode|key themes|in this conversation|topics covered)\b\s*(?:,[^:]*)?:", text)[0].strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in sentences:
        if len(out) + len(s) > limit and out:
            break
        out = (out + " " + s).strip()
    if len(out) > limit:
        out = out[: limit - 1].rsplit(" ", 1)[0] + "…"
    return out[0].upper() + out[1:]


def parse_duration(v):
    if not v:
        return "", ""
    v = v.strip()
    if v.isdigit():
        secs = int(v)
    else:
        parts = [int(p) for p in v.split(":") if p.isdigit()]
        secs = 0
        for p in parts:
            secs = secs * 60 + p
    if secs <= 0:
        return "", ""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    iso = "PT" + (f"{h}H" if h else "") + (f"{m}M" if m else "") + (f"{s}S" if s else "")
    return iso, f"{round(secs / 60)} min"


def parse_rss(xml_text):
    root = ET.fromstring(xml_text.encode("utf-8"))
    ch = root.find("channel")
    show_img_el = ch.find("itunes:image", NS)
    show_img = show_img_el.get("href") if show_img_el is not None else ""
    items = []
    for it in ch.findall("item"):
        g = lambda tag: (it.findtext(tag, default="", namespaces=NS) or "").strip()
        pub = g("pubDate")
        date = ""
        if pub:
            try:
                date = email.utils.parsedate_to_datetime(pub).date().isoformat()
            except Exception:
                date = ""
        iso, label = parse_duration(g("itunes:duration"))
        img_el = it.find("itunes:image", NS)
        img = img_el.get("href") if img_el is not None else ""
        if img == show_img:
            img = ""
        items.append({
            "guid": g("guid"),
            "rss_title": html.unescape(g("title")),
            "link": g("link"),
            "date": date,
            "iso": iso,
            "dur": label,
            "image": img,
            "description": g("description") or g("itunes:summary"),
        })
    items.sort(key=lambda x: x["date"] or "0000")  # oldest first
    return items


def parse_youtube(xml_text):
    root = ET.fromstring(xml_text.encode("utf-8"))
    vids = []
    for e in root.findall("atom:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        title = e.findtext("atom:title", default="", namespaces=NS)
        group = e.find("media:group", NS)
        views = 0
        if group is not None:
            st = group.find("media:community/media:statistics", NS)
            if st is not None and st.get("views", "").isdigit():
                views = int(st.get("views"))
        vids.append({"id": vid, "title": html.unescape(title), "views": views,
                     "published": (e.findtext("atom:published", default="", namespaces=NS) or "")[:10]})
    return vids


# ── Helpers for new episodes ────────────────────────────────────────────────
NOISE = re.compile(r"(?i)\s*[|\-–—:]+\s*((the )?flying high club( podcast| afa)?( x .*)?|fhc\b.*|series \d+.*|afa 20\d\d.*|aviation festival asia)\s*$")


def clean_title(t):
    t = re.sub(r'["“”]', "", t).strip()
    t = re.sub(r"(?i)\s+FHC\b.*$", "", t)
    for _ in range(3):
        t = NOISE.sub("", t).strip()
    return t.strip('"“” ')


NAME = re.compile(r"\b([A-Z][a-zA-Z'’\-]+(?:\s+(?:[A-Z][a-zA-Z'’\-]+|de|del|van|von|bin|ben|al|el)){1,3})\b")
NOT_NAMES = {"The Flying", "Flying High", "High Club", "World Aviation", "Aviation Festival"}


def guess_guest(title, desc):
    """Best-effort guest name: 'Title - Guest Name - Role' or 'with Guest Name'."""
    title = re.sub(r'["“”]', "", title)
    parts = re.split(r"\s+[|\-–—]\s+", title)
    for p in parts[1:] + parts[:1]:
        m = re.match(r"^([A-Z][\w'’\-]+(?:\s+[A-Z][\w'’\-]+){1,2})\s+(?=%s\b)" % ROLE_WORDS, p.strip())
        if m and m.group(1) not in NOT_NAMES:
            return m.group(1)
    for p in parts[1:] + parts[:1]:
        p = p.strip(' "“”')
        m = NAME.fullmatch(p.split(",")[0].strip()) if p else None
        if m and m.group(1) not in NOT_NAMES and len(p.split()) <= 4:
            return m.group(1)
    m = re.search(r"\bwith ([A-Z][\w'’\-]+(?: [A-Z][\w'’\-]+){1,3})", title + " " + strip_html(desc)[:400])
    if m:
        return m.group(1)
    return ""


ROLE_WORDS = r"(?:CEO|CTO|COO|CFO|CIO|CCO|CMO|CPO|Founder|Co-Founder|President|Director|Vice President|VP|SVP|EVP|Head|Chief|Managing|Partner|Chairman|Chair|General Manager)"


def split_title(raw_title, guest):
    """Return (clean title, role, company) by removing the guest/role segments from the title."""
    title = clean_title(raw_title)
    if not guest:
        return title, "", ""
    parts = [p.strip(' "“”') for p in re.split(r"\s+[|\-–—]\s+", title)]
    keep, role, company = [], "", ""
    for p in parts:
        if guest.lower() in p.lower():
            rest = re.sub(re.escape(guest), "", p, flags=re.I).strip(" ,-–—:")
            if rest and re.search(ROLE_WORDS, rest):
                p = rest
            else:
                continue
        if re.match(ROLE_WORDS, p) and len(p.split()) <= 8:
            m = re.match(r"^(.*?%s[^,]*?)\s*(?:,|\s+of\s+|\s+at\s+)\s*(.+)$" % ROLE_WORDS, p)
            m2 = re.match(r"^((?:%s)(?:\s*&\s*%s)?)\s+([A-Z].+)$" % (ROLE_WORDS, ROLE_WORDS), p)
            if m:
                role, company = m.group(1).strip(), m.group(2).strip(" .")
            elif m2:
                role, company = m2.group(1).strip(), m2.group(2).strip(" .")
            else:
                role = p.strip(" .")
            continue
        keep.append(p)
    t = " - ".join(keep).strip(' "“”') or title
    t = re.sub(r"\s+with\s+%s\s*$" % re.escape(guest), "", t, flags=re.I)
    return t, role, company


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower())


def iso_dur_seconds(v):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", v or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def youtube_api_videos(key):
    """All uploads on the channel with duration, via the YouTube Data API (needs YOUTUBE_API_KEY)."""
    uploads = "UU" + YT_CHANNEL_ID[2:]
    items, token = [], ""
    for _ in range(20):                                  # up to 1,000 videos
        url = (f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet,contentDetails&maxResults=50"
               f"&playlistId={uploads}&key={key}" + (f"&pageToken={token}" if token else ""))
        j = json.loads(fetch(url))
        for it in j.get("items", []):
            sn = it["snippet"]
            if sn.get("title") in ("Private video", "Deleted video"):
                continue
            items.append({"id": it["contentDetails"]["videoId"], "title": html.unescape(sn.get("title", "")),
                          "description": sn.get("description", ""),
                          "published": (it["contentDetails"].get("videoPublishedAt") or sn.get("publishedAt") or "")[:10]})
        token = j.get("nextPageToken")
        if not token:
            break
    for i in range(0, len(items), 50):
        ids = ",".join(v["id"] for v in items[i:i + 50])
        j = json.loads(fetch(f"https://www.googleapis.com/youtube/v3/videos?part=contentDetails,statistics,snippet&id={ids}&key={key}"))
        info = {v["id"]: v for v in j.get("items", [])}
        for v in items[i:i + 50]:
            x = info.get(v["id"], {})
            v["seconds"] = iso_dur_seconds(x.get("contentDetails", {}).get("duration", ""))
            v["views"] = int(x.get("statistics", {}).get("viewCount", 0) or 0)
            v["live"] = x.get("snippet", {}).get("liveBroadcastContent", "none") != "none"
    return items


def spotify_episodes(cid, secret):
    """Spotify listener-page links for each episode (needs SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET)."""
    import base64
    req = urllib.request.Request("https://accounts.spotify.com/api/token", data=b"grant_type=client_credentials",
                                 headers={**UA, "Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode(),
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read())["access_token"]
    out, url = [], f"https://api.spotify.com/v1/shows/{SPOTIFY_SHOW_ID}/episodes?market=GB&limit=50"
    while url:
        rq = urllib.request.Request(url, headers={**UA, "Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(rq, timeout=30) as r:
            j = json.loads(r.read())
        out += [{"title": e.get("name", ""), "url": e.get("external_urls", {}).get("spotify", "")} for e in j.get("items", []) if e]
        url = j.get("next")
    return out


def best_title_match(title, guest, candidates, key="title", threshold=0.72):
    best, score = None, 0.0
    for c in candidates:
        r = difflib.SequenceMatcher(None, norm(title), norm(c[key])).ratio()
        if guest and norm(guest) and norm(guest) in norm(c[key]):
            r += 0.25
        if r > score:
            best, score = c, r
    return best if score >= threshold else None


def match_youtube(ep_title, guest, vids, used):
    best, score = None, 0.0
    for v in vids:
        if v["id"] in used:
            continue
        r = difflib.SequenceMatcher(None, norm(ep_title), norm(v["title"])).ratio()
        if guest and norm(guest) in norm(v["title"]):
            r += 0.25
        if r > score:
            best, score = v, r
    return best if score >= 0.72 else None


def download_image(url, dest):
    if NO_DOWNLOAD or dest.exists():
        return dest.exists()
    try:
        data = fetch(url, binary=True)
        if len(data) < 2000:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(data)).convert("RGB")
            im.thumbnail((900, 900))
            im.save(dest, "JPEG", quality=84, optimize=True, progressive=True)
        except Exception:
            dest.write_bytes(data)
        log("saved image", dest.relative_to(ROOT))
        return True
    except Exception as e:
        log("image download failed:", url, e)
        return False


# ── HTML builders ───────────────────────────────────────────────────────────
E = html.escape


def nice_date(d):
    if not d:
        return ""
    x = dt.date.fromisoformat(d)
    return f"{x.day} {x.strftime('%b %Y')}"


def initials(name):
    w = [x for x in re.split(r"\s+", name) if x and x[0].isalpha()]
    return "".join(x[0] for x in w[:2]).upper() or "FHC"


def who(ep):
    return ", ".join(x for x in [ep["role"], ep["company"]] if x)


def platform_buttons(ep, cls="plat"):
    out = []
    if ep.get("youtube"):
        out.append(f'<a class="{cls} {cls}-yt" href="{E(ep["youtube"])}" target="_blank" rel="noopener">'
                   f'<span class="plat-ico plat-ico-play" aria-hidden="true"></span>Watch on YouTube</a>')
    if ep.get("spotify"):
        out.append(f'<a class="{cls} {cls}-sp" href="{E(ep["spotify"])}" target="_blank" rel="noopener">'
                   f'<span class="plat-ico plat-ico-ear" aria-hidden="true"></span>Listen on Spotify</a>')
    elif ep.get("youtubeOnly"):
        out.append(f'<span class="{cls} {cls}-soon">Spotify soon</span>')
    return "".join(out)


def build_guide(eps):
    rows = []
    for ep in reversed(eps):
        meta = " · ".join(x for x in [nice_date(ep["date"]), ep["dur"]] if x)
        w = who(ep)
        rows.append(f'''      <li class="ep-item">
        <div class="ep-num">EP {ep["number"]:02d}</div>
        <div class="ep-main">
          <h3 class="ep-title"><a href="{E(ep["watch"])}" target="_blank" rel="noopener">{E(ep["title"])}</a></h3>
          <p class="ep-guest"><strong>{E(ep["guest"] or "The Flying High Club")}</strong>{(" — " + E(w)) if w else ""}</p>
          {f'<p class="ep-sum">{E(ep["summary"])}</p>' if ep["summary"] else ""}
          <div class="ep-plats">{platform_buttons(ep, "ep-plat")}</div>
        </div>
        <div class="ep-meta">{E(meta)}</div>
      </li>''')
    return ('    <ol class="ep-list" id="epList">\n' + "\n".join(rows) + "\n    </ol>\n"
            f'    <button type="button" class="btn btn-ghost ep-more" id="epMore" aria-expanded="false" aria-controls="epList">Show all {len(eps)} episodes</button>')


def build_latest(ep):
    art = (f'<img src="{E(file_version(ep["photo"]))}" alt="{E(ep["guest"])}" style="{"object-position:" + E(ep["photoPosition"]) if ep.get("photoPosition") else ""}">'
           if ep["photo"] else f'<div class="latest-silhouette">{E(initials(ep["guest"]))}</div>')
    w = who(ep)
    blurb = ep["summary"] or ep["title"]
    return f'''
    <div class="section-header">
      <div>
        <span class="eyebrow eyebrow-brass">The latest flight</span>
        <h2 class="section-title">Episode {ep["number"]} — <em>Now streaming</em></h2>
      </div>
      <a href="#episodes" class="section-link">All episodes →</a>
    </div>

    <div class="latest-card">
      <div class="latest-art{" wide" if ep.get("duo") else ""}" data-ep="EP {ep["number"]}">
        {art}
      </div>
      <div class="latest-meta">
        <span class="eyebrow">A conversation with</span>
        <h3 class="latest-title">{E(ep["title"])}</h3>
        <blockquote class="latest-quote">
          {E(blurb)}
        </blockquote>
        <div class="latest-guest">
          <div class="guest-chip">{E(initials(ep["guest"]))}</div>
          <div>
            <div class="guest-name">{E(ep["guest"])}</div>
            {f'<div class="guest-role">{E(w)}</div>' if w else ""}
          </div>
        </div>
        <div class="latest-ctas">
          {platform_buttons(ep, "lplat")}
        </div>
      </div>
    </div>
'''


FAQ = [
    ("What is The Flying High Club?",
     "The Flying High Club is an aviation industry video podcast. Each episode is a long-form conversation with a leader shaping aviation — airline CEOs, airport executives, founders, technologists and investors — about strategy, leadership, technology, sustainability and careers in the industry. It launched in 2023 and is the Official Podcast Partner of the World Aviation Festival."),
    ("Who has been a guest on The Flying High Club?", None),  # built from episode data
    ("Where can I watch or listen to The Flying High Club?",
     "Full video episodes are on YouTube. Audio episodes are on Spotify, Apple Podcasts and Amazon Music, and the show is also active on LinkedIn."),
    ("Who is the podcast for?",
     "People who work in or around aviation and travel: airline and airport professionals, travel-tech and aerospace teams, investors, agencies and partners, and anyone considering a career in the industry who wants to hear directly from its leaders."),
    ("Is The Flying High Club an airline or airport podcast?",
     "Both, and more. Episodes cover airlines, airports, ground handling, loyalty, travel technology, sustainable aviation fuel, AI and aviation startups, so it suits anyone looking for an airline podcast, an airport podcast or a broader aviation and travel industry podcast."),
    ("How can my company sponsor the show or appear as a guest?",
     "Use the Advertise form to discuss sponsorship formats, from intro reads to dedicated Spotlight episodes and season partnerships, or the Be a guest form to suggest yourself or a colleague. Both are linked at the bottom of this page."),
]


def guests_answer(eps):
    named = [ep for ep in reversed(eps) if ep["guest"] and not ep.get("auto")]
    bits = []
    for ep in named[:10]:
        w = who(ep)
        bits.append(f'{ep["guest"]}{(" (" + w + ")") if w else ""}')
    return "Guests include " + "; ".join(bits) + "." if bits else "Leaders from across airlines, airports and aviation technology."


def build_jsonld(eps):
    same = [LINKS[k] for k in ["youtube", "spotify", "apple", "amazon", "linkedin", "instagram", "tiktok", "x"]]
    graph = [
        {"@type": "Organization", "@id": SITE_URL + "#org", "name": "The Flying High Club", "url": SITE_URL,
         "logo": {"@type": "ImageObject", "url": SITE_URL + "logo.png", "width": 512, "height": 512},
         "sameAs": same, "email": CONTACT_EMAIL},
        {"@type": "WebSite", "@id": SITE_URL + "#website", "url": SITE_URL, "name": "The Flying High Club",
         "inLanguage": "en-GB", "publisher": {"@id": SITE_URL + "#org"}},
        {"@type": "PodcastSeries", "@id": SITE_URL + "#podcast", "name": "The Flying High Club",
         "alternateName": "The Flying High Club Podcast", "url": SITE_URL,
         "description": "The Flying High Club is an aviation industry video podcast featuring in-depth conversations with airline CEOs, airport leaders, aviation founders, technologists and investors. Official Podcast Partner of the World Aviation Festival.",
         "image": SITE_URL + "og-image.jpg", "webFeed": RSS_URL, "inLanguage": "en-GB",
         "genre": ["Aviation", "Business", "Travel industry"],
         "keywords": "aviation podcast, aviation video podcast, airline podcast, airport podcast, aviation industry podcast, travel industry podcast",
         "startDate": "2023-09-03", "publisher": {"@id": SITE_URL + "#org"}, "author": {"@id": SITE_URL + "#org"},
         "sameAs": same},
    ]
    for ep in eps:
        node = {"@type": "PodcastEpisode", "name": ep["title"], "url": ep["spotify"] if ep.get("spotify") and ep["spotify"] != LINKS["spotify"] else ep["watch"],
                "episodeNumber": ep["number"], "partOfSeries": {"@id": SITE_URL + "#podcast"}, "inLanguage": "en-GB"}
        if ep["summary"]:
            node["description"] = ep["summary"]
        if ep["date"]:
            node["datePublished"] = ep["date"]
        if ep["iso"]:
            node["timeRequired"] = ep["iso"]
        if ep["youtube"]:
            node["video"] = {"@type": "VideoObject", "name": ep["title"], "embedUrl": ep["youtube"].replace("watch?v=", "embed/"),
                             "url": ep["youtube"], "thumbnailUrl": f'https://i.ytimg.com/vi/{ep["youtube"].split("v=")[-1]}/hqdefault.jpg',
                             **({"uploadDate": ep["date"]} if ep["date"] else {}),
                             **({"description": ep["summary"]} if ep["summary"] else {})}
        actors = ep.get("actors") or []
        if not actors and ep["guest"]:
            names = [n.strip() for n in ep["guest"].split("&")]
            for n in names:
                a = {"@type": "Person", "name": n}
                if len(names) == 1:
                    if ep["role"]:
                        a["jobTitle"] = ep["role"]
                    if ep["company"]:
                        a["worksFor"] = {"@type": "Organization", "name": ep["company"]}
                actors.append(a)
        if actors:
            node["actor"] = actors[0] if len(actors) == 1 else actors
        graph.append(node)
    faq = [(q, a if a else guests_answer(eps)) for q, a in FAQ]
    graph.append({"@type": "FAQPage", "@id": SITE_URL + "#faq",
                  "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]})
    body = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, indent=1).replace("</", "<\\/")
    return '<script type="application/ld+json">\n' + body + "\n</script>"


def build_js(eps, live):
    latest = eps[-1]
    js_eps = []
    for ep in reversed(eps):
        js_eps.append({"number": f'{ep["number"]:02d}', "title": ep["title"], "guest": ep["guest"] or ep["company"] or "The Flying High Club",
                       "role": ep["role"], "company": ep["company"], "photo": file_version(ep["photo"]) if ep["photo"] else "", "photoPosition": ep.get("photoPosition", ""),
                       "quote": ep["quote"], "topics": ep["topics"], "url": ep["watch"], "youtube": ep["youtube"], "spotify": ep.get("spotify", ""),
                       **({"youtubeOnly": True} if ep.get("youtubeOnly") else {}),
                       **({"featured": True} if ep.get("featured") else {}),
                       **({"duo": True} if ep.get("duo") else {})})
    data = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "latestEpisode": {"title": latest["title"], "meta": " · ".join(x for x in [f'EP {latest["number"]}', latest["dur"].upper()] if x),
                          "url": latest["watch"]},
        "episodes": js_eps,
    }
    if live:
        data["liveStats"] = live
    data["logos"] = web_logos()
    return "const AUTO = " + json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/") + ";"


def build_llms(eps):
    lines = []
    for ep in reversed(eps):
        w = who(ep)
        lines.append(f'- Episode {ep["number"]}: {ep["title"]} — {ep["guest"]}{(", " + w) if w else ""}'
                     f'{(" (" + nice_date(ep["date"]) + ")") if ep["date"] else ""}. {ep["summary"]} Watch/listen: {ep["watch"]}')
    faq = "\n\n".join(f"### {q}\n{a if a else guests_answer(eps)}" for q, a in FAQ)
    return f"""# The Flying High Club

> The Flying High Club is an aviation industry video podcast: long-form conversations with airline CEOs, airport leaders, aviation founders, technologists and investors. Launched in 2023. Official Podcast Partner of the World Aviation Festival.

Website: {SITE_URL}
Video: {LINKS['youtube']}
Audio: {LINKS['spotify']} · {LINKS['apple']} · {LINKS['amazon']}
LinkedIn: {LINKS['linkedin']}
RSS feed: {RSS_URL}

## Topics
Airlines and airline strategy, airports and airport innovation, ground handling, airline loyalty, travel technology and AI, sustainable aviation and SAF, aviation startups and venture capital, leadership and aviation careers.

## Episodes ({len(eps)})
{chr(10).join(lines)}

## Frequently asked questions
{faq}

## Contact
Sponsorship, guest suggestions and press: {SITE_URL}#advertise · {SITE_URL}#be-a-guest · {SITE_URL}#press
"""


def replace_block(text, name, new, js=False):
    if js:
        pat = re.compile(r"(/\*AUTO:%s:START\*/\n).*?(\n/\*AUTO:%s:END\*/)" % (name, name), re.S)
    else:
        pat = re.compile(r"(<!--AUTO:%s:START-->).*?(<!--AUTO:%s:END-->)" % (name, name), re.S)
    if not pat.search(text):
        raise SystemExit(f"Marker AUTO:{name} not found in index.html — was it edited by hand?")
    return pat.sub(lambda m: m.group(1) + new + m.group(2), text, count=1)


def file_version(rel):
    """Short content hash for cache-busting: photos/logos get ?v=… that changes when the image changes."""
    import hashlib
    f = SITE / rel.split("?")[0]
    try:
        return rel.split("?")[0] + "?v=" + hashlib.md5(f.read_bytes()).hexdigest()[:8]
    except Exception:
        return rel


def web_logos():
    """Clean white, transparent versions of every logo for the carousel (site/logos/web/)."""
    src_dir, out_dir = SITE / "logos", SITE / "logos" / "web"
    if not src_dir.exists():
        return {}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from style_photos import white_logo
    except Exception as e:
        log("logo cleaner unavailable:", e)
        white_logo = None
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest, keep = {}, set()
    for f in sorted(src_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in {".png", ".webp", ".jpg", ".jpeg"}:
            continue
        key = re.sub(r"[^a-z0-9]+", "-", f.stem.lower()).strip("-")
        rel = f"logos/{f.name}"
        if white_logo:
            dest = out_dir / (key + ".png")
            try:
                img = white_logo(f, max_w=480, max_h=200)
                import io
                buf = io.BytesIO(); img.save(buf, "PNG", optimize=True)
                if not dest.exists() or dest.read_bytes() != buf.getvalue():
                    dest.write_bytes(buf.getvalue())
                rel = f"logos/web/{dest.name}"
                keep.add(dest.name)
            except Exception as e:
                log("could not clean logo", f.name, e)
        manifest[key] = file_version(rel)
    for old in out_dir.glob("*.png"):
        if old.name not in keep:
            old.unlink()
    return manifest


def trim_logos():
    """Crop empty transparent padding from logo files so they display at a sensible size."""
    d = SITE / "logos"
    if not d.exists():
        return
    try:
        from PIL import Image
    except ImportError:
        return
    for f in d.iterdir():
        if f.suffix.lower() not in {".png", ".webp"}:
            continue
        try:
            im = Image.open(f)
            if im.mode not in ("RGBA", "LA", "P"):
                continue
            im = im.convert("RGBA")
            box = im.split()[-1].point(lambda v: 255 if v > 8 else 0).getbbox()
            if box and (box[2] - box[0] < im.width - 12 or box[3] - box[1] < im.height - 12):
                pad = 4
                box = (max(0, box[0] - pad), max(0, box[1] - pad), min(im.width, box[2] + pad), min(im.height, box[3] + pad))
                im.crop(box).save(f)
                log("trimmed logo padding:", f.name)
        except Exception as e:
            log("could not trim", f.name, e)


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    trim_logos()
    # 1. Feeds
    try:
        rss_text = Path(RSS_FILE).read_text() if RSS_FILE else fetch(RSS_URL)
        items = parse_rss(rss_text)
    except Exception as e:
        log("Could not read the RSS feed, leaving the site unchanged:", e)
        return 0
    if not items:
        log("RSS feed had no episodes, leaving the site unchanged.")
        return 0
    key = os.environ.get("YOUTUBE_API_KEY")
    api_vids = None
    if key:
        try:
            api_vids = youtube_api_videos(key)
            log(f"YouTube API: {len(api_vids)} videos on the channel")
        except Exception as e:
            log("YouTube API unavailable, falling back to the public feed:", e)
    if api_vids is not None:
        vids = [v for v in api_vids if v["seconds"] >= FULL_EPISODE_MIN_MINUTES * 60 and not v["live"]]
    else:
        try:
            vids = parse_youtube(Path(YT_FILE).read_text() if YT_FILE else fetch(YT_FEED_URL))
        except Exception as e:
            log("YouTube feed unavailable (episodes will link to Spotify):", e)
            vids = []

    live = None
    if key:
        try:
            j = json.loads(fetch(f"https://www.googleapis.com/youtube/v3/channels?part=statistics&id={YT_CHANNEL_ID}&key={key}"))
            st = j["items"][0]["statistics"]
            live = {"views": int(st.get("viewCount", 0)), "subscribers": int(st.get("subscriberCount", 0)),
                    "videos": int(st.get("videoCount", 0))}
            log("live YouTube stats:", live)
        except Exception as e:
            log("YouTube API stats unavailable:", e)

    spot = []
    if os.environ.get("SPOTIFY_CLIENT_ID") and os.environ.get("SPOTIFY_CLIENT_SECRET"):
        try:
            spot = spotify_episodes(os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"])
            log(f"Spotify API: {len(spot)} episodes")
        except Exception as e:
            log("Spotify API unavailable (Listen buttons will open the show page):", e)

    # 2. Merge with hand-edited data
    data = json.loads(DATA.read_text()) if DATA.exists() else []
    by_guid = {d["guid"]: d for d in data if d.get("guid")}
    yt_id = lambda u: (u or "").split("v=")[-1].split("&")[0] if u else ""
    used_vids = {yt_id(d["youtube"]) for d in data if d.get("youtube")}
    vid_by_id = {v["id"]: v for v in vids}
    eps, added = [], 0

    def make_ep(d, *, title, desc, date, iso, dur, link, image):
        ep = {
            "number": int(d["number"]) if d.get("number") else 0,
            "title": d.get("title") or clean_title(title),
            "guest": d.get("guest", ""), "role": d.get("role", ""), "company": d.get("company", ""),
            "summary": d.get("summary") or auto_summary(desc),
            "quote": d.get("quote", ""), "topics": d.get("topics", []),
            "photo": d.get("photo", ""), "photoPosition": d.get("photoPosition", ""),
            "youtube": d.get("youtube", ""), "auto": d.get("auto", False), "duo": d.get("duo", False),
            "date": date, "iso": iso, "dur": dur, "link": link, "image": image, "d": d,
            "spotify": d.get("spotify", ""), "youtubeOnly": bool(d.get("youtubeOnly")), "featured": bool(d.get("featured")),
        }
        if ep["guest"] and "Gary McDonald" in ep["guest"]:
            ep["actors"] = [
                {"@type": "Person", "name": "Gary McDonald", "jobTitle": "President North America", "worksFor": {"@type": "Organization", "name": "Air Sheriff"}},
                {"@type": "Person", "name": "Maurice Jenkins", "jobTitle": "Chief Innovation Officer", "worksFor": {"@type": "Organization", "name": "Miami International Airport"}}]
        return ep

    # 2a. podcast feed episodes
    for it in items:
        d = by_guid.get(it["guid"])
        if d is None:
            d = next((x for x in data if x.get("match") and x["match"].lower() in it["rss_title"].lower()), None)
        if d is None:   # an episode that was on YouTube first has now reached the podcast feed
            yt_only = [x for x in data if x.get("youtubeOnly") and not x.get("guid")]
            cands = [{"title": x.get("ytTitle") or x.get("title", ""), "d": x} for x in yt_only]
            hit = best_title_match(it["rss_title"], guess_guest(it["rss_title"], it["description"]), cands)
            if hit:
                d = hit["d"]
                d.pop("youtubeOnly", None)
                log(f'"{d.get("title")}" is now on the podcast feed too — linked, no duplicate created')
        if d is not None and not d.get("guid"):
            d["guid"] = it["guid"]
        if d is None:
            guest = guess_guest(it["rss_title"], it["description"])
            t, role, company = split_title(it["rss_title"], guest)
            d = {"guid": it["guid"], "auto": True, "title": t,
                 "guest": guest, "role": role, "company": company,
                 "summary": auto_summary(it["description"]), "quote": "", "topics": [],
                 "photo": "", "photoPosition": "", "youtube": ""}
            data.append(d)
            added += 1
            log(f'NEW episode: "{d["title"]}" (guest guess: {d["guest"] or "?"})')
        ep = make_ep(d, title=it["rss_title"], desc=it["description"], date=it["date"], iso=it["iso"],
                     dur=it["dur"], link=it["link"], image=it["image"])
        if not ep["youtube"] and vids:
            v = match_youtube(it["rss_title"], ep["guest"], vids, used_vids)
            if v:
                ep["youtube"] = d["youtube"] = f'https://www.youtube.com/watch?v={v["id"]}'
                used_vids.add(v["id"])
                log(f'matched YouTube video: {v["title"]}')
        eps.append(ep)

    # 2b. full-length YouTube videos that aren't on the podcast feed yet
    if YOUTUBE_FIRST_EPISODES and api_vids is not None:
        for v in sorted(vids, key=lambda v: v["published"]):
            d = next((x for x in data if yt_id(x.get("youtube")) == v["id"]), None)
            if d is not None and not d.get("youtubeOnly"):
                continue                                   # already a podcast episode
            if d is None:
                if v["published"] < YOUTUBE_ONLY_SINCE:
                    continue
                guest = guess_guest(v["title"], v["description"])
                t, role, company = split_title(v["title"], guest)
                d = {"youtubeOnly": True, "auto": True, "ytTitle": v["title"], "title": t,
                     "ytPublished": v["published"], "ytSeconds": v["seconds"],
                     "guest": guest, "role": role, "company": company,
                     "summary": auto_summary(v["description"]), "quote": "", "topics": [],
                     "photo": "", "photoPosition": "", "youtube": f'https://www.youtube.com/watch?v={v["id"]}'}
                data.append(d)
                added += 1
                log(f'NEW YouTube episode (not on Spotify yet): "{t}" (guest guess: {guest or "?"})')
            d["ytPublished"], d["ytSeconds"] = v["published"], v["seconds"]     # keep fresh for key-less runs
            used_vids.add(v["id"])
            mins = round(v["seconds"] / 60)
            iso = f'PT{v["seconds"] // 3600}H{v["seconds"] % 3600 // 60}M' if v["seconds"] >= 3600 else f'PT{v["seconds"] // 60}M'
            eps.append(make_ep(d, title=v["title"], desc=v["description"], date=v["published"], iso=iso,
                               dur=f"{mins} min", link="", image=""))

    if YOUTUBE_FIRST_EPISODES and api_vids is not None:
        by_id = {v["id"]: v for v in api_vids}
        shown = {yt_id(e["youtube"]) for e in eps if e.get("youtube")}
        for d in data:
            vid = yt_id(d.get("youtube"))
            if not d.get("youtubeOnly") or not vid or vid in shown:
                continue
            v = by_id.get(vid)
            if v:
                d["ytPublished"], d["ytSeconds"] = v["published"], v["seconds"]
                d.setdefault("ytTitle", v["title"])
            sec = int(d.get("ytSeconds") or 0)
            iso = (f"PT{sec // 3600}H{sec % 3600 // 60}M" if sec >= 3600 else f"PT{sec // 60}M") if sec else ""
            eps.append(make_ep(d, title=d.get("ytTitle") or d.get("title", ""), desc=(v or {}).get("description", ""),
                               date=d.get("ytPublished", ""), iso=iso, dur=f"{round(sec / 60)} min" if sec else "",
                               link="", image=""))
    elif YOUTUBE_FIRST_EPISODES:
        # no YouTube key in this run (e.g. the photo workflow): keep the saved YouTube-only episodes on the site
        for d in data:
            if not d.get("youtubeOnly") or not d.get("youtube") or not d.get("ytPublished"):
                continue                       # date not known yet; the next run with the key fills it in
            sec = int(d.get("ytSeconds") or 0)
            iso = (f"PT{sec // 3600}H{sec % 3600 // 60}M" if sec >= 3600 else f"PT{sec // 60}M") if sec else ""
            eps.append(make_ep(d, title=d.get("ytTitle") or d.get("title", ""), desc="", date=d.get("ytPublished", ""),
                               iso=iso, dur=f"{round(sec / 60)} min" if sec else "", link="", image=""))

    # episodes you've hidden ("hidden": true in data/episodes.json) are left out completely
    eps = [e for e in eps if not e["d"].get("hidden")]

    # 2c. numbering by publish date (your "number" overrides win), links, artwork
    eps.sort(key=lambda e: (e["date"] or "0000", e["number"]))
    n = 0
    for ep in eps:
        n = ep["number"] if ep["number"] else n + 1
        ep["number"] = n
    eps.sort(key=lambda e: e["number"])

    for ep in eps:
        d = ep.pop("d")
        if not ep["spotify"] and spot and not ep["youtubeOnly"]:
            hit = best_title_match(d.get("ytTitle") or ep["title"], ep["guest"], spot)
            if hit and hit["url"]:
                ep["spotify"] = d["spotify"] = hit["url"]
        if not ep["spotify"] and not ep["youtubeOnly"]:
            ep["spotify"] = LINKS["spotify"]             # show page until the exact episode link is known
        # artwork (stable file names, so re-numbering never mixes guests up)
        art = d.get("artwork")
        if not art or not (SITE / art).exists():
            ident = re.sub(r"[^a-zA-Z0-9]+", "", (d.get("guid") or yt_id(ep["youtube"]) or str(ep["number"])))[:24]
            dest = AUTO_IMG_DIR / f"art-{ident}.jpg"
            vid = yt_id(ep["youtube"])
            src = ep["image"] or (f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg" if vid else "")
            ok = dest.exists() or bool(src and download_image(src, dest))
            if not ok and vid and not ep["image"]:
                ok = download_image(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", dest)
            if ok:
                d["artwork"] = f"guests/auto/{dest.name}"
        if not ep["photo"] and USE_AUTO_ARTWORK and d.get("artwork"):
            ep["photo"] = d["artwork"]
        ep["watch"] = ep["youtube"] or ep["spotify"] or ep["link"] or LINKS["spotify"]

    # 3. Rewrite the site
    page = INDEX.read_text()
    before = page
    latest = eps[-1]
    page = replace_block(page, "JSONLD", "\n" + build_jsonld(eps) + "\n")
    page = replace_block(page, "EYEBROW", f'<span class="eyebrow">Episode {latest["number"]} — Now streaming</span>')
    page = replace_block(page, "LATEST", build_latest(latest))
    page = replace_block(page, "COUNT", str(len(eps)))
    page = replace_block(page, "GUIDE", "\n" + build_guide(eps) + "\n")
    page = replace_block(page, "DATA", build_js(eps, live), js=True)

    # timestamps alone shouldn't count as a change
    strip_ts = lambda s: re.sub(r'"updated": "[^"]*"', "", s)
    changed = strip_ts(page) != strip_ts(before)
    if changed:
        INDEX.write_text(page)
        (SITE / "llms.txt").write_text(build_llms(eps))
        today = dt.date.today().isoformat()
        sm = SITE / "sitemap.xml"
        sm.write_text(re.sub(r"<lastmod>[^<]*</lastmod>", f"<lastmod>{today}</lastmod>", sm.read_text()))
        log(f"site updated: {len(eps)} episodes, latest = EP {latest['number']} \"{latest['title']}\"")
    else:
        log("no changes")
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    if added:
        log(f"{added} new episode(s) added to data/episodes.json — edit guest/role/quote there whenever you like.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
