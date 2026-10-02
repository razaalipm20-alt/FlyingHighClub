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
NOISE = re.compile(r"(?i)\s*[|\-–—:]\s*(the flying high club|fhc\b.*|flying high club x .*|series \d+.*)$")


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
            m = re.match(r"^(.*?%s[^,]*?)\s+(?:of|at|,)\s+(.+)$" % ROLE_WORDS, p)
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
        </div>
        <div class="ep-meta">{E(meta)}</div>
      </li>''')
    return ('    <ol class="ep-list" id="epList">\n' + "\n".join(rows) + "\n    </ol>\n"
            f'    <button type="button" class="btn btn-ghost ep-more" id="epMore" aria-expanded="false" aria-controls="epList">Show all {len(eps)} episodes</button>')


def build_latest(ep):
    art = (f'<img src="{E(ep["photo"])}" alt="{E(ep["guest"])}" style="{"object-position:" + E(ep["photoPosition"]) if ep.get("photoPosition") else ""}">'
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
      <div class="latest-art" data-ep="EP {ep["number"]}">
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
          <a class="btn btn-primary" data-latest-link href="{E(ep["watch"])}" target="_blank" rel="noopener">
            <span class="play-icon"></span>
            Play episode
          </a>
          <a class="btn-dark" href="{E(ep["link"] or ep["watch"])}" target="_blank" rel="noopener">Show notes</a>
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
        node = {"@type": "PodcastEpisode", "name": ep["title"], "url": ep["link"] or ep["watch"],
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
        js_eps.append({"number": f'{ep["number"]:02d}', "title": ep["title"], "guest": ep["guest"] or "The Flying High Club",
                       "role": ep["role"], "company": ep["company"], "photo": ep["photo"], "photoPosition": ep.get("photoPosition", ""),
                       "quote": ep["quote"], "topics": ep["topics"], "url": ep["link"] or LINKS["spotify"], "youtube": ep["youtube"]})
    data = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "latestEpisode": {"title": latest["title"], "meta": " · ".join(x for x in [f'EP {latest["number"]}', latest["dur"].upper()] if x),
                          "url": latest["watch"]},
        "episodes": js_eps,
    }
    if live:
        data["liveStats"] = live
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


# ── Main ────────────────────────────────────────────────────────────────────
def main():
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
    try:
        vids = parse_youtube(Path(YT_FILE).read_text() if YT_FILE else fetch(YT_FEED_URL))
    except Exception as e:
        log("YouTube feed unavailable (episodes will link to Spotify):", e)
        vids = []

    live = None
    key = os.environ.get("YOUTUBE_API_KEY")
    if key:
        try:
            j = json.loads(fetch(f"https://www.googleapis.com/youtube/v3/channels?part=statistics&id={YT_CHANNEL_ID}&key={key}"))
            st = j["items"][0]["statistics"]
            live = {"views": int(st.get("viewCount", 0)), "subscribers": int(st.get("subscriberCount", 0)),
                    "videos": int(st.get("videoCount", 0))}
            log("live YouTube stats:", live)
        except Exception as e:
            log("YouTube API stats unavailable:", e)

    # 2. Merge with hand-edited data
    data = json.loads(DATA.read_text()) if DATA.exists() else []
    by_guid = {d["guid"]: d for d in data if d.get("guid")}
    used_vids = {d["youtube"].split("v=")[-1] for d in data if d.get("youtube")}
    eps, added = [], 0
    for n, it in enumerate(items, start=1):
        d = by_guid.get(it["guid"])
        if d is None:
            d = next((x for x in data if x.get("match") and x["match"].lower() in it["rss_title"].lower()), None)
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

        ep = {
            "number": int(d.get("number") or n),
            "title": d.get("title") or clean_title(it["rss_title"]),
            "guest": d.get("guest", ""), "role": d.get("role", ""), "company": d.get("company", ""),
            "summary": d.get("summary") or auto_summary(it["description"]),
            "quote": d.get("quote", ""), "topics": d.get("topics", []),
            "photo": d.get("photo", ""), "photoPosition": d.get("photoPosition", ""),
            "youtube": d.get("youtube", ""), "auto": d.get("auto", False),
            "date": it["date"], "iso": it["iso"], "dur": it["dur"], "link": it["link"],
        }
        if ep["guest"] and "Gary McDonald" in ep["guest"]:
            ep["actors"] = [
                {"@type": "Person", "name": "Gary McDonald", "jobTitle": "President North America", "worksFor": {"@type": "Organization", "name": "Air Sheriff"}},
                {"@type": "Person", "name": "Maurice Jenkins", "jobTitle": "Chief Innovation Officer", "worksFor": {"@type": "Organization", "name": "Miami International Airport"}}]

        # YouTube match (only fills the link if you haven't set one)
        if not ep["youtube"] and vids:
            v = match_youtube(it["rss_title"], ep["guest"], vids, used_vids)
            if v:
                ep["youtube"] = f'https://www.youtube.com/watch?v={v["id"]}'
                used_vids.add(v["id"])
                d["youtube"] = ep["youtube"]
                log(f'matched YouTube video for EP {ep["number"]}: {v["title"]}')

        # Artwork: your photo > RSS episode art > YouTube thumbnail
        if not ep["photo"]:
            dest = AUTO_IMG_DIR / f'ep-{ep["number"]:02d}.jpg'
            src = it["image"] or (f'https://i.ytimg.com/vi/{ep["youtube"].split("v=")[-1]}/maxresdefault.jpg' if ep["youtube"] else "")
            ok = dest.exists() or (src and download_image(src, dest))
            if not ok and ep["youtube"] and not it["image"]:
                ok = download_image(f'https://i.ytimg.com/vi/{ep["youtube"].split("v=")[-1]}/hqdefault.jpg', dest)
            if ok:
                ep["photo"] = f'guests/auto/ep-{ep["number"]:02d}.jpg'

        ep["watch"] = ep["youtube"] or ep["link"] or LINKS["spotify"]
        eps.append(ep)

    eps.sort(key=lambda e: e["number"])

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
