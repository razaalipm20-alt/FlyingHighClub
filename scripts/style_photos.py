#!/usr/bin/env python3
"""
Style guest photos for the roster.

Drop a guest's photo into the `photos/` folder, named after the guest
(e.g. photos/steven-greenway.jpg). This script (run automatically by
.github/workflows/style-photos.yml) will:
  1. cut the person out of the background
  2. make them black & white on the FHC navy backdrop with a brass glow (900x1200)
  3. save it as site/guests/<name>.jpg
  4. set that photo on the matching episode in data/episodes.json
  5. move the original into photos/done/
Then it runs the site updater so the roster refreshes.
"""
import json, os, re, shutil, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance, ImageOps

ROOT = Path(__file__).resolve().parent.parent
PHOTOS, DONE, OUT = ROOT / "photos", ROOT / "photos" / "done", ROOT / "site" / "guests"
DATA = ROOT / "data" / "episodes.json"
W, H = 900, 1200
NAVY, DEEP, BRASS = np.array([14, 42, 71]), np.array([8, 11, 16]), np.array([183, 146, 104])


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def backdrop(cx, cy):
    y, x = np.mgrid[0:H, 0:W].astype(float)
    r = np.sqrt(((x - cx) / (W * 0.75)) ** 2 + ((y - cy) / (H * 0.6)) ** 2)
    base = DEEP + (NAVY - DEEP) * np.clip(1 - r, 0, 1)[..., None] ** 1.2
    halo = np.exp(-(((x - cx) / 260) ** 2 + ((y - cy) / 260) ** 2))[..., None]
    img = base + BRASS * 0.22 * halo
    grid = ((x % 26 < 1.6) & (y % 26 < 1.6))[..., None]
    img = np.where(grid, img + 10, img)
    return Image.fromarray(np.clip(img, 0, 255).astype("uint8"))


LOGOS = ROOT / "site" / "logos"


STOP = {"the", "of", "and", "plc", "ltd", "limited", "inc", "co", "group", "uk", "holdings", "llc", "sa", "ag"}
LOGO_EXTS = {".png", ".webp", ".jpg", ".jpeg"}
# words that often appear in downloaded logo file names but aren't part of the company name
LOGO_NOISE = {"logo", "logos", "logotype", "white", "black", "colour", "color", "colours", "transparent", "rgb", "cmyk",
              "hd", "hires", "final", "official", "icon", "mark", "horizontal", "vertical", "primary", "secondary",
              "dark", "light", "small", "large", "full", "copy", "new", "brand", "web", "png", "svg", "reversed", "mono"}


def logo_tokens(key):
    return [t for t in tokens(key) if t not in LOGO_NOISE and not re.fullmatch(r"v?\d+[a-z]?", t)]


def tokens(text):
    return [t for t in slug(text).split("-") if t and t not in STOP]


def all_logos():
    if not LOGOS.exists():
        return {}
    return {slug(f.stem): f for f in sorted(LOGOS.iterdir()) if f.suffix.lower() in LOGO_EXTS}


def match_logo(text, logos):
    """Best logo whose words all appear in `text` (most specific wins). Returns (file, logo_key) or (None, None)."""
    words = set(tokens(text))
    if not words:
        return None, None
    best, best_key, best_score = None, None, 0
    for key, f in logos.items():
        lt = logo_tokens(key)
        if lt and set(lt) <= words:
            score = len(lt) * 10 + (5 if " ".join(lt) in " ".join(tokens(text)) else 0)
            if score > best_score:
                best, best_key, best_score = f, key, score
    return best, best_key


def person_context(person, ep):
    """The part of an episode's role/company text that belongs to this person (handles two-guest episodes)."""
    guests = guests_of(ep)
    role, company = ep.get("role", ""), ep.get("company", "")
    if len(guests) > 1:
        parts = [x.strip() for x in re.split(r"\s*[·|;]\s*", role) if x.strip()]
        for i, g in enumerate(guests):
            if (g == person or g in person) and i < len(parts):
                return parts[i], "role"
    if company:
        return company, "company"
    return role, "role"


def resolve_logo(person, hint, eps, logos):
    """Returns (logo file or None, explanation)."""
    if hint:
        if hint in {"none", "no-logo", "nologo"}:
            return None, "turned off in file name"
        f, key = match_logo(hint, logos)
        if f:
            return f, f"from file name (--{hint})"
        return None, f"file name asks for '{hint}' but site/logos has no matching file"
    for ep in eps:
        if ep.get("logo"):
            f, key = match_logo(ep["logo"], logos)
            return (f, "from episodes.json \"logo\"") if f else (None, f"episodes.json asks for '{ep['logo']}' but no matching file")
    for ep in eps:
        ctx, where = person_context(person, ep)
        f, key = match_logo(ctx, logos)
        if f:
            return f, f"matched {where}: {ctx}"
    company = ""
    for ep in eps:
        ctx, where = person_context(person, ep)
        if where == "company" or len(guests_of(ep)) > 1:
            company = ctx.split(",")[-1].strip() if where == "role" else ctx
            break
    if company:
        return None, f"no logo yet for '{company}' — add site/logos/{slug(company)}.png"
    return None, "episode has no company — add one in data/episodes.json or name the photo person--logo.jpg"


def white_logo(path, max_w=210, max_h=100):
    """Turn any logo into a white mark. Dark parts become solid white, light parts (e.g. a grey
    plane over blue letters) become softer white, so internal details stay visible."""
    lg = Image.open(path).convert("RGBA")
    a = np.array(lg).astype(float)
    if a[..., 3].min() == 255:  # no transparency: treat near-white as background
        bgmask = (a[..., :3].sum(axis=2) > 720)
        a[..., 3] = np.where(bgmask, 0, 255)
    lum = (0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]) / 255
    a[..., 3] = a[..., 3] * np.clip(1.3 - lum, 0.42, 1.0)
    a[..., :3] = 255
    lg = Image.fromarray(a.astype("uint8"))
    box = lg.split()[-1].point(lambda v: 255 if v > 8 else 0).getbbox()
    lg = lg.crop(box) if box else lg
    lg.thumbnail((max_w, max_h), Image.LANCZOS)
    return lg


def add_logo(img, logo_path, slot_right=None):
    lg = white_logo(logo_path)
    alpha = lg.split()[-1].point(lambda v: int(v * 0.92))
    lg.putalpha(alpha)
    # top-right: open backdrop there, clear of the guest and of the episode badge (top-left on the card)
    x = (slot_right or W) - lg.width - 56
    y = 72
    shadow = Image.new("RGBA", lg.size, (0, 0, 0, 0)); shadow.putalpha(alpha.point(lambda v: int(v * 0.6)))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)), (x + 2, y + 3))
    img.alpha_composite(lg, (x, y))
    return img


def prepare_subject(src, session):
    """Cut the person out and return a styled black & white RGBA subject plus its key measurements."""
    from rembg import remove
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    im.thumbnail((2400, 2400))
    cut = remove(im, session=session)
    a = np.array(cut.split()[-1])
    ys, xs = np.where(a > 40)
    if not len(ys):
        raise ValueError("no person found in photo")
    top, bottom = ys.min(), ys.max()
    band = a[top: top + max(1, (bottom - top) // 4)] > 40
    bx = np.where(band.any(axis=0))[0]
    rgb, alpha = cut.convert("RGB"), cut.split()[-1]
    g = ImageOps.grayscale(rgb)
    g = g.point([int(255 * ((i / 255) ** 1.12) * 0.92 + 6) for i in range(256)])
    g = ImageEnhance.Contrast(g).enhance(1.06)
    g = ImageEnhance.Sharpness(g).enhance(1.25)
    return {"img": Image.merge("RGBA", (g, g, g, alpha)), "top": top, "bottom": bottom,
            "face_x": (bx.min() + bx.max()) / 2, "face_w": max(1, bx.max() - bx.min()), "width": xs.max() - xs.min(),
            "reaches_bottom": bottom >= im.height - 3}


def place(canvas, subj, slot_x, slot_w, head_w=None):
    """Scale and position one subject inside a slot of the canvas (head ~12% from the top).
    head_w: force a head width in px, so several people in one image appear the same size."""
    span = max(1, subj["bottom"] - subj["top"])
    if head_w:
        s = head_w / subj["face_w"]
    else:
        s = (H * 0.88) / span if subj["reaches_bottom"] else (H * 0.80) / span
        s = max(s, (slot_w * 0.85) / max(1, subj["width"]))
        s = min(s, (H * 1.6) / span)   # never zoom so far that the head leaves the frame
    cut = subj["img"].resize((max(1, int(subj["img"].width * s)), max(1, int(subj["img"].height * s))), Image.LANCZOS)
    top_s = int(subj["top"] * s)
    ox = int(slot_x + slot_w / 2 - subj["face_x"] * s)
    oy = int(H * 0.12) - top_s
    if subj["reaches_bottom"] and oy + int(subj["bottom"] * s) < H:
        oy = H - int(subj["bottom"] * s) - 1
    alpha = cut.split()[-1]
    sh = Image.new("RGBA", cut.size, (0, 0, 0, 0))
    sh.putalpha(alpha.point(lambda v: int(v * 0.55)))
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(18)), (ox + 10, oy + 14))
    canvas.alpha_composite(cut, (ox, oy))
    return oy + top_s


def compose(subjects, logos, dst):
    """1 subject -> 900x1200 portrait. 2+ subjects -> side by side, 900px per person, one backdrop."""
    n = len(subjects)
    cw = W * n
    # one backdrop panel per person, side by side (each with its own glow behind the head)
    canvas = Image.new("RGBA", (cw, H))
    part = backdrop(W / 2, int(H * 0.12) + 170).convert("RGBA")
    for i in range(n):
        canvas.alpha_composite(part, (i * W, 0))
    head_w = W * 0.30 if n > 1 else None    # same head size for everyone in a group image
    for i, subj in enumerate(subjects):
        place(canvas, subj, i * W, W, head_w)
        if logos[i]:
            add_logo(canvas, logos[i], slot_right=(i + 1) * W)
    arr = np.array(canvas.convert("RGB")).astype(float)
    yy = np.linspace(0, 1, H)[:, None]
    fade = np.clip((yy - 0.82) / 0.18, 0, 1) * 0.55
    arr = arr * (1 - fade[..., None]) + DEEP * fade[..., None]
    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr.astype("uint8")).save(dst, "JPEG", quality=86, optimize=True, progressive=True)


def guests_of(ep):
    return [slug(g) for g in re.split(r"\s*&\s*|\s+and\s+", ep.get("guest", "")) if g.strip()]


def episode_for(name, data):
    return [ep for ep in data if name in guests_of(ep) or any(g and g in name for g in guests_of(ep))]


def summary(rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = ["| Photo | Episode | Logo | Why |", "|---|---|---|---|"]
    for r in rows:
        lines.append("| " + " | ".join(x.replace("|", "/") for x in r) + " |")
    text = "## Guest photos\n\n" + "\n".join(lines) + "\n"
    print(text)
    if path:
        with open(path, "a") as fh:
            fh.write(text)


def main():
    exts = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
    new = [p for p in PHOTOS.iterdir() if p.is_file() and p.suffix.lower() in exts] if PHOTOS.exists() else []
    done = [p for p in DONE.iterdir() if p.is_file() and p.suffix.lower() in exts] if DONE.exists() else []
    if not new and not done:
        print("[photos] nothing to process")
        return 0
    from rembg import new_session
    session = new_session("isnet-general-use")
    data = json.loads(DATA.read_text())
    logos = all_logos()
    rows = []
    by_person = {}
    for f in done + new:                       # newer uploads win
        person, _, hint = f.stem.partition("--")
        by_person[slug(person)] = (f, slug(hint))

    subjects, person_logo = {}, {}
    for person, (f, hint) in by_person.items():
        eps = episode_for(person, data)
        logo, why = resolve_logo(person, hint, eps, logos)
        try:
            subjects[person] = prepare_subject(f, session)
        except Exception as e:
            rows.append((f.name, "—", "—", f"could not process: {e}"))
            continue
        person_logo[person] = logo
        compose([subjects[person]], [logo], OUT / f"{person}.jpg")     # every guest gets a portrait
        if f.parent == PHOTOS:
            DONE.mkdir(parents=True, exist_ok=True)
            for old in DONE.glob("*"):
                if old.is_file() and slug(old.stem.partition("--")[0]) == person:
                    old.unlink()
            shutil.move(str(f), str(DONE / f.name))
        rows.append((f.name, eps[0].get("title", "") if eps else "no episode matched — check the name",
                     logo.name if logo else "none", why))

    # attach photos to episodes; episodes with several guests get one side-by-side image
    for ep in data:
        guests = guests_of(ep)
        have = [g for g in guests if g in subjects]
        if not have:
            continue
        if len(guests) > 1 and len(have) == len(guests):
            name = slug(ep.get("title", "") or "-".join(guests))[:60]
            compose([subjects[g] for g in guests], [person_logo.get(g) for g in guests], OUT / f"{name}.jpg")
            ep["photo"], ep["duo"] = f"guests/{name}.jpg", True
            rows.append(("(" + " + ".join(guests) + ")", ep.get("title", ""), "each guest's own", "side-by-side image for a multi-guest episode"))
        else:
            ep["photo"] = f"guests/{have[0]}.jpg"
            ep.pop("duo", None)
            if len(guests) > 1:
                waiting = [g for g in guests if g not in subjects]
                rows.append(("—", ep.get("title", ""), "—", "showing " + have[0] + " for now; upload " + ", ".join(w + ".jpg" for w in waiting) + " for the side-by-side card"))
    DATA.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
