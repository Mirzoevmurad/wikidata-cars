#!/usr/bin/env python3
"""
enrich_autoru.py — auxiliary scraper for auto.ru's catalog.

Status: auto.ru is reachable from any IP, but the Yandex anti-bot stack
("SmartCaptcha") fronts every catalog URL and returns a "Вы не робот?"
HTML stub when it does not recognise the visitor as Russian or trusted.
For this reason the scraper currently can NOT do useful work from a
non-RU server: every page returns the captcha challenge.

What this module nevertheless ships:
    - The full URL-discovery and spec-parsing pipeline against auto.ru's
      published HTML structure (catalog index → brand → model → generation
      → trim → /specifications/ page with a <dl> spec list).
    - A ``CaptchaError`` raised whenever the captcha stub is detected, so
      the operator immediately knows their IP isn't accepted.
    - A ``--proxy`` argument plus ``HTTPS_PROXY`` env-var support so the
      scraper can be run through a Russian residential / data-centre proxy
      without code changes when one is available.

When the user redeploys on a Russian VPS (or wires up a residential proxy)
this module will start producing real output. Until then, expect every
``crawl()`` to return zero new rows and log a single CaptchaError.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests
from tqdm import tqdm

BASE = "https://auto.ru"
CATALOG = f"{BASE}/catalog/cars/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("autoru")


class CaptchaError(RuntimeError):
    """Raised when auto.ru returns the SmartCaptcha challenge instead of content."""


_CAPTCHA_MARKERS = (
    "Вы не робот",
    "captcha",
    "smart_captcha",
    "showcaptcha",
)


def _is_captcha(html: str) -> bool:
    return any(marker in html for marker in _CAPTCHA_MARKERS) and "<title" in html and len(html) < 30_000


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def fetch(session: requests.Session, url: str, retries: int = 3) -> str | None:
    last_err: str | None = None
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                text = r.text
                if _is_captcha(text):
                    raise CaptchaError(f"captcha challenge at {url}")
                return text
            if r.status_code == 404:
                return None
            last_err = f"HTTP {r.status_code}"
        except CaptchaError:
            raise
        except requests.RequestException as e:
            last_err = repr(e)
        time.sleep(2 ** attempt)
    log.debug("fetch failed for %s: %s", url, last_err)
    return None


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------


def list_brands(session: requests.Session) -> list[str]:
    """Return absolute brand URLs like https://auto.ru/catalog/cars/toyota/."""
    html = fetch(session, CATALOG) or ""
    out: list[str] = []
    for m in re.finditer(
        r'href="(/catalog/cars/([a-z][a-z0-9_\-]+)/)"', html
    ):
        full = m.group(1)
        out.append(BASE + full)
    return list(dict.fromkeys(out))


def list_models(session: requests.Session, brand_url: str) -> list[str]:
    """Return absolute model URLs like /catalog/cars/toyota/camry/."""
    html = fetch(session, brand_url) or ""
    brand_slug = brand_url.rstrip("/").rsplit("/", 1)[-1]
    out: list[str] = []
    pat = re.compile(
        rf'href="(/catalog/cars/{re.escape(brand_slug)}/[a-z][a-z0-9_\-]+/)"'
    )
    for m in pat.finditer(html):
        out.append(BASE + m.group(1))
    return list(dict.fromkeys(out))


def list_generations(session: requests.Session, model_url: str) -> list[str]:
    """Return absolute generation URLs (with numeric gen id segment)."""
    html = fetch(session, model_url) or ""
    out: list[str] = []
    # Auto.ru gen URLs look like /catalog/cars/<brand>/<model>/<digits>/
    pat = re.compile(
        r'href="(/catalog/cars/[^"/]+/[^"/]+/(\d+)/)"'
    )
    for m in pat.finditer(html):
        out.append(BASE + m.group(1))
    return list(dict.fromkeys(out))


def list_trims(session: requests.Session, gen_url: str) -> list[str]:
    """Return absolute trim spec-page URLs."""
    html = fetch(session, gen_url) or ""
    out: list[str] = []
    # Auto.ru spec-page URLs end with /specifications/
    pat = re.compile(r'href="(/catalog/cars/[^"]+/specifications/)"')
    for m in pat.finditer(html):
        out.append(BASE + m.group(1))
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Spec extraction from a /specifications/ page
# ---------------------------------------------------------------------------


# auto.ru's specifications page renders the spec block as a definition list:
#     <dt class="...">Длина</dt><dd class="...">4885 мм</dd>
# (the exact CSS classes change but the dt/dd alternation does not.)
_DT_DD = re.compile(
    r"<dt[^>]*>\s*([^<]{1,60}?)\s*</dt>\s*<dd[^>]*>(.*?)</dd>",
    re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean(value: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", value).replace("\xa0", " ")).strip()


# Map auto.ru labels (russian) to our DB columns.
LABEL_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^длина\b", re.I),                "length_mm"),
    (re.compile(r"^ширина\b", re.I),               "width_mm"),
    (re.compile(r"^высота\b", re.I),               "height_mm"),
    (re.compile(r"кол[её]сная\s+база", re.I),      "wheelbase_mm"),
    (re.compile(r"снаряж\w*\s+масса", re.I),       "mass_kg"),
    (re.compile(r"^мощность\b", re.I),             "power_w"),
    (re.compile(r"объ[её]м\s+двиг", re.I),         "engine_displacement_cc"),
    (re.compile(r"^привод\b", re.I),               "drive_type"),
    (re.compile(r"^топливо\b", re.I),              "fuel_type"),
    (re.compile(r"количество\s+двер", re.I),       "doors"),
    (re.compile(r"^тип\s+кузова\b", re.I),         "body_style"),
    (re.compile(r"^кузов\b", re.I),                "body_style"),
    (re.compile(r"^трансмиссия\b", re.I),          "transmission"),
    (re.compile(r"коробка(\s+передач)?", re.I),    "transmission"),
    (re.compile(r"максимальная\s+скорость", re.I), "max_speed"),
    (re.compile(r"количество\s+мест", re.I),       "seats"),
]


_NUM = re.compile(r"(-?\d+(?:[.,]\d+)?)")


def _to_int(s: str) -> int | None:
    m = _NUM.search(s.replace(" ", ""))
    return int(float(m.group(1).replace(",", "."))) if m else None


def _to_float(s: str) -> float | None:
    m = _NUM.search(s.replace(" ", ""))
    return float(m.group(1).replace(",", ".")) if m else None


def _to_mm(value: str) -> float | None:
    m = re.search(r"(\d{3,5}(?:[.,]\d+)?)\s*мм", value.replace("\xa0", " ").lower())
    return float(m.group(1).replace(",", ".")) if m else None


def _to_kg(value: str) -> float | None:
    m = re.search(r"(\d{3,5}(?:[.,]\d+)?)\s*кг", value.replace("\xa0", " ").lower())
    return float(m.group(1).replace(",", ".")) if m else None


def _to_w(value: str) -> float | None:
    v = value.replace("\xa0", " ").lower()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*л\.?\s*с", v)
    if m:
        return float(m.group(1).replace(",", ".")) * 735.5
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*квт", v)
    if m:
        return float(m.group(1).replace(",", ".")) * 1000
    return None


def _to_cc(value: str) -> float | None:
    v = value.replace("\xa0", " ").lower()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*л\b", v)
    if m:
        liters = float(m.group(1).replace(",", "."))
        if 0.3 < liters < 12:
            return round(liters * 1000, 1)
    m = re.search(r"(\d{3,5})\s*(см³|см3|cc)", v)
    return float(m.group(1)) if m else None


CONVERTERS = {
    "length_mm":              _to_mm,
    "width_mm":               _to_mm,
    "height_mm":              _to_mm,
    "wheelbase_mm":           _to_mm,
    "mass_kg":                _to_kg,
    "power_w":                _to_w,
    "engine_displacement_cc": _to_cc,
    "doors":                  _to_int,
    "max_speed":              _to_int,
}


def parse_specifications(html: str) -> dict:
    """Extract specs from a /specifications/ HTML page."""
    out: dict = {}
    for m in _DT_DD.finditer(html):
        label = _clean(m.group(1))
        value = _clean(m.group(2))
        if not label or not value:
            continue
        for pat, col in LABEL_RULES:
            if pat.search(label):
                conv = CONVERTERS.get(col)
                cooked = conv(value) if conv else value
                if cooked in (None, "", 0):
                    continue
                out.setdefault(col, cooked)
                break
    return out


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


_FILLABLE = (
    "length_mm",
    "width_mm",
    "height_mm",
    "wheelbase_mm",
    "mass_kg",
    "power_w",
    "engine_displacement_cc",
    "fuel_type",
    "drive_type",
    "doors",
    "body_style",
    "transmission",
    "max_speed",
    "engine",
    "production_start",
    "discontinued",
)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    have = {row[1] for row in conn.execute("PRAGMA table_info(cars)").fetchall()}
    for col, kind in (
        ("autoru_url", "TEXT"),
        ("source_specs", "TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE cars ADD COLUMN {col} {kind}")
    conn.commit()


_NORM_RE = re.compile(r"[^a-z0-9а-я]+", re.IGNORECASE)


def _normalize(s: str | None) -> str:
    return _NORM_RE.sub("", s.lower()) if s else ""


@dataclass
class ExistingRow:
    qid: str
    label: str
    manufacturer: str | None


def _build_match_index(conn: sqlite3.Connection) -> dict[str, ExistingRow]:
    rows = conn.execute("SELECT qid, label, manufacturer FROM cars").fetchall()
    idx: dict[str, ExistingRow] = {}
    for qid, label, manuf in rows:
        if not label:
            continue
        cands: set[str] = {label}
        if manuf and label.lower().startswith(manuf.lower() + " "):
            cands.add(label[len(manuf):].strip())
        no_paren = re.sub(r"\s*\([^)]*\)", "", label).strip()
        if no_paren:
            cands.add(no_paren)
        row = ExistingRow(qid, label, manuf)
        for c in cands:
            cn = _normalize(c)
            if cn:
                idx.setdefault(cn, row)
            if manuf:
                idx.setdefault(_normalize(manuf + c), row)
    return idx


def _match(idx: dict[str, ExistingRow], brand: str, model: str) -> ExistingRow | None:
    # Only match by full brand+model — see enrich_drom._match for the rationale
    # (short model names like "A1"/"3" collide across manufacturers).
    for key in (
        _normalize(brand + " " + model),
        _normalize(brand + model),
    ):
        if key and key in idx:
            row = idx[key]
            if not row.manufacturer or _normalize(row.manufacturer) == _normalize(brand) \
               or _normalize(brand) in _normalize(row.manufacturer) \
               or _normalize(row.manufacturer) in _normalize(brand):
                return row
    return None


def upsert(
    conn: sqlite3.Connection,
    qid: str,
    brand: str,
    model: str,
    specs: dict,
    url: str,
) -> bool:
    existing = conn.execute(
        f"SELECT label, manufacturer, source_specs, {', '.join(_FILLABLE)} FROM cars WHERE qid=?",
        (qid,),
    ).fetchone()

    if existing is None:
        cols = ["qid", "label", "manufacturer", "autoru_url", "source_specs", "updated_at"] + list(_FILLABLE)
        vals: list = [qid, model, brand, url, "auto.ru", time.strftime("%Y-%m-%d %H:%M:%S")]
        for f in _FILLABLE:
            vals.append(specs.get(f))
        conn.execute(
            f"INSERT INTO cars ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
            vals,
        )
        return True

    _label, _manuf, cur_source_specs, *cur_vals = existing
    cur = dict(zip(_FILLABLE, cur_vals))
    sets: list[str] = ["autoru_url=?"]
    params: list = [url]
    src_marks: list[str] = []
    for f, new_val in specs.items():
        if f not in _FILLABLE or new_val in (None, "", 0):
            continue
        if cur.get(f) in (None, "", 0):
            sets.append(f"{f}=?")
            params.append(new_val)
            src_marks.append(f)
    if len(sets) > 1:
        sets.append("source_specs = COALESCE(NULLIF(source_specs,''),'') || ? ")
        params.append(("," if cur_source_specs else "") + "auto.ru:" + ",".join(src_marks))
        sets.append("updated_at=datetime('now')")
        params.append(qid)
        conn.execute(f"UPDATE cars SET {', '.join(sets)} WHERE qid=?", params)
        return True

    conn.execute("UPDATE cars SET autoru_url=? WHERE qid=?", (url, qid))
    return False


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


@dataclass
class TrimTask:
    brand_slug: str
    brand_human: str
    model_slug: str
    model_human: str
    trim_url: str


def _slug_to_human(slug: str) -> str:
    return slug.replace("_", " ").replace("-", " ").title()


def crawl(
    db_path: Path,
    brand_limit: int = 0,
    model_limit: int = 0,
    workers: int = 4,
    proxy: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_columns(conn)
    idx = _build_match_index(conn)
    log.info("matched index: %d entries", len(idx))

    bootstrap = requests.Session()
    if proxy:
        bootstrap.proxies = {"http": proxy, "https": proxy}

    try:
        brands = list_brands(bootstrap)
    except CaptchaError as e:
        log.error("auto.ru rejected this IP with captcha: %s", e)
        log.error("Use --proxy <ru-proxy-url> or run from a Russian VPS to scrape auto.ru.")
        conn.close()
        return

    log.info("brands found: %d", len(brands))
    if brand_limit:
        brands = brands[:brand_limit]

    tls = threading.local()

    def _session() -> requests.Session:
        s = getattr(tls, "session", None)
        if s is None:
            s = requests.Session()
            if proxy:
                s.proxies = {"http": proxy, "https": proxy}
            tls.session = s
        return s

    # Discover models
    log.info("listing models...")
    model_tasks: list[tuple[str, str, str, str]] = []
    captcha_hit = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def _list(burl: str):
            nonlocal captcha_hit
            slug = burl.rstrip("/").rsplit("/", 1)[-1]
            human = _slug_to_human(slug)
            try:
                models = list_models(_session(), burl)
            except CaptchaError:
                captcha_hit = True
                return []
            if model_limit:
                models = models[:model_limit]
            out = []
            for m in models:
                m_slug = m.rstrip("/").rsplit("/", 1)[-1]
                out.append((slug, human, m_slug, m))
            return out
        for fut in tqdm([pool.submit(_list, b) for b in brands], desc="brands"):
            try:
                model_tasks.extend(fut.result())
            except Exception as e:
                log.debug("brand listing failed: %r", e)
    if captcha_hit:
        log.error("captcha hit during model listing — aborting auto.ru pass")
        conn.close()
        return
    log.info("models discovered: %d", len(model_tasks))

    # Discover trim spec URLs (one trim per model: take the latest gen, first trim)
    log.info("listing trims...")
    trim_tasks: list[TrimTask] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def _trim(t):
            bs, bh, ms, murl = t
            try:
                gens = list_generations(_session(), murl)
                if not gens:
                    return None
                trims = list_trims(_session(), gens[0])
                if not trims:
                    return None
                return TrimTask(bs, bh, ms, _slug_to_human(ms), trims[0])
            except CaptchaError:
                return None
        for fut in tqdm([pool.submit(_trim, t) for t in model_tasks], desc="models"):
            r = fut.result()
            if r:
                trim_tasks.append(r)
    log.info("trims to crawl: %d", len(trim_tasks))

    inserted = 0
    updated = 0
    db_lock = threading.Lock()

    log.info("crawling specs...")

    def _spec(t: TrimTask):
        try:
            html = fetch(_session(), t.trim_url)
        except CaptchaError:
            return None
        if not html:
            return None
        return t, parse_specifications(html)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in tqdm([pool.submit(_spec, t) for t in trim_tasks], desc="specs"):
            r = fut.result()
            if not r:
                continue
            task, specs = r
            if not specs:
                continue
            existing = _match(idx, task.brand_human, task.model_human)
            qid = existing.qid if existing else f"autoru:{task.brand_slug}-{task.model_slug}"
            with db_lock:
                try:
                    changed = upsert(conn, qid, task.brand_human, task.model_human, specs, task.trim_url)
                    if changed:
                        if existing:
                            updated += 1
                        else:
                            inserted += 1
                except sqlite3.Error as e:
                    log.warning("DB error %r", e)
                if (inserted + updated) % 200 == 0:
                    conn.commit()

    conn.commit()
    conn.close()
    log.info("done: inserted %d new, updated %d existing", inserted, updated)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).parent / "data" / "cars.db"))
    ap.add_argument("--brand-limit", type=int, default=0)
    ap.add_argument("--model-limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--proxy",
        default=os.environ.get("HTTPS_PROXY") or os.environ.get("AUTORU_PROXY"),
        help="HTTPS proxy URL (e.g. http://user:pass@host:port). Required when "
             "running from outside Russia because auto.ru challenges every "
             "non-RU IP with SmartCaptcha.",
    )
    args = ap.parse_args()
    crawl(
        Path(args.db),
        brand_limit=args.brand_limit,
        model_limit=args.model_limit,
        workers=args.workers,
        proxy=args.proxy,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
