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
        lt = tokens(key)
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
    """Turn any logo into a clean white mark (keeps its shape, drops its colours)."""
    lg = Image.open(path).convert("RGBA")
    a = np.array(lg)
    if a[..., 3].min() == 255:  # no transparency: treat near-white as background
        bgmask = (a[..., :3].astype(int).sum(axis=2) > 720)
        a[..., 3] = np.where(bgmask, 0, 255)
    a[..., :3] = 255
    lg = Image.fromarray(a)
    lg = lg.crop(lg.getbbox())
    lg.thumbnail((max_w, max_h), Image.LANCZOS)
    return lg


def add_logo(img, logo_path):
    lg = white_logo(logo_path)
    alpha = lg.split()[-1].point(lambda v: int(v * 0.92))
    lg.putalpha(alpha)
    # top-right: open backdrop there, clear of the guest and of the episode badge (top-left on the card)
    x = W - lg.width - 56
    y = 72
    shadow = Image.new("RGBA", lg.size, (0, 0, 0, 0)); shadow.putalpha(alpha.point(lambda v: int(v * 0.6)))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)), (x + 2, y + 3))
    img.alpha_composite(lg, (x, y))
    return img


def style(src, dst, session, logo=None):
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
    face_x = (bx.min() + bx.max()) / 2
    reaches_bottom = bottom >= im.height - 3
    # head at ~12% from the top; if the photo is cropped at the waist, fill to the bottom
    s = (H * 0.88) / max(1, (bottom - top)) if reaches_bottom else (H * 0.80) / max(1, (bottom - top))
    s = max(s, (W * 0.85) / max(1, (xs.max() - xs.min())))  # shoulders roughly fill the width
    cut = cut.resize((max(1, int(cut.width * s)), max(1, int(cut.height * s))), Image.LANCZOS)
    top_s = int(top * s)
    ox = int(W / 2 - face_x * s)
    oy = int(H * 0.12) - top_s
    if reaches_bottom and oy + int(bottom * s) < H:
        oy = H - int(bottom * s) - 1
    rgb, alpha = cut.convert("RGB"), cut.split()[-1]
    g = ImageOps.grayscale(rgb)
    g = g.point([int(255 * ((i / 255) ** 1.12) * 0.92 + 6) for i in range(256)])
    g = ImageEnhance.Contrast(g).enhance(1.06)
    g = ImageEnhance.Sharpness(g).enhance(1.25)
    subj = Image.merge("RGBA", (g, g, g, alpha))
    bg = backdrop(W / 2, oy + top_s + 170).convert("RGBA")
    sh = Image.new("RGBA", subj.size, (0, 0, 0, 0))
    sh.putalpha(alpha.point(lambda v: int(v * 0.55)))
    bg.alpha_composite(sh.filter(ImageFilter.GaussianBlur(18)), (ox + 10, oy + 14))
    bg.alpha_composite(subj, (ox, oy))
    yy = np.linspace(0, 1, H)[:, None]
    fade = np.clip((yy - 0.82) / 0.18, 0, 1) * 0.55
    arr = np.array(bg.convert("RGB")).astype(float)
    arr = arr * (1 - fade[..., None]) + DEEP * fade[..., None]
    final = Image.fromarray(arr.astype("uint8")).convert("RGBA")
    if logo:
        final = add_logo(final, logo)
    dst.parent.mkdir(parents=True, exist_ok=True)
    final.convert("RGB").save(dst, "JPEG", quality=86, optimize=True, progressive=True)


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
    # Newer uploads win if the same person appears in both folders
    by_person = {}
    for f in done + new:
        person, _, hint = f.stem.partition("--")
        by_person[slug(person)] = (f, slug(hint))
    for person, (f, hint) in by_person.items():
        eps = episode_for(person, data)
        logo, why = resolve_logo(person, hint, eps, logos)
        out = OUT / f"{person}.jpg"
        try:
            style(f, out, session, logo)
        except Exception as e:
            rows.append((f.name, "—", "—", f"could not process: {e}"))
            continue
        for ep in eps:
            ep["photo"] = f"guests/{person}.jpg"
        if f.parent == PHOTOS:
            DONE.mkdir(parents=True, exist_ok=True)
            for old in DONE.glob(f"{person}*"):          # replace an older original for the same person
                if slug(old.stem.partition("--")[0]) == person:
                    old.unlink()
            shutil.move(str(f), str(DONE / f.name))
        rows.append((f.name, eps[0].get("title", "") if eps else "no episode matched — check the name",
                     logo.name if logo else "none", why))
    DATA.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
