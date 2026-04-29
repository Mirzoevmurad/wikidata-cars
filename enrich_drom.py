#!/usr/bin/env python3
"""
enrich_drom.py — auxiliary scraper for drom.ru's catalog.

Drom.ru is reachable from non-RU IPs (the current Frankfurt VPS works), so
this scraper runs anywhere. The site is a Next.js-style React app with
server-side-rendered HTML; specs are exposed as label/value `<span>` pairs
inside CSS-in-JS-hashed classes. We extract them with class-agnostic regexes.

URL hierarchy:
    /catalog/                                   – all brands
    /catalog/<brand>/                           – all models for a brand
    /catalog/<brand>/<model>/                   – all generations
    /catalog/<brand>/<model>/g_<gen_id>/        – one generation's overview
                                                  (this page already has the
                                                  spec block we need)

Strategy: walk brand → model → generation → take the most recent generation
and lift the structured label/value pairs plus prose-fallback regexes for
"Колёсная база Camry — 2775 мм" type sentences.

Like ``enrich_autodata.py``, this module fuzzy-matches each model back to
existing rows in the ``cars`` table and only fills empty cells. New models
not in Wikidata are inserted with a synthetic ``drom:<gen_id>`` qid.
"""
from __future__ import annotations

import argparse
import logging
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

BASE = "https://www.drom.ru"
CATALOG = f"{BASE}/catalog/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.7",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("drom")


# ---------------------------------------------------------------------------
# HTTP layer (cp1251-aware)
# ---------------------------------------------------------------------------


def fetch(session: requests.Session, url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                # Drom serves cp1251 on some pages and utf-8 on others; let
                # requests guess via its `encoding` heuristic, then fall back.
                if r.encoding and r.encoding.lower() in ("iso-8859-1", "windows-1251"):
                    try:
                        return r.content.decode("cp1251", errors="replace")
                    except UnicodeDecodeError:
                        pass
                try:
                    return r.content.decode("utf-8")
                except UnicodeDecodeError:
                    return r.content.decode("cp1251", errors="replace")
            if r.status_code == 404:
                return None
            log.debug("http %s for %s (attempt %d)", r.status_code, url, attempt + 1)
        except requests.RequestException as e:
            log.debug("network error %r for %s", e, url)
        time.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------


def list_brands(session: requests.Session) -> list[str]:
    """Return absolute brand URLs like https://www.drom.ru/catalog/toyota/."""
    html = fetch(session, CATALOG) or ""
    out: list[str] = []
    # Drom emits both relative (/catalog/x/) and absolute (https://www.drom.ru/catalog/x/) hrefs.
    for m in re.finditer(
        r'href="(?:https?://(?:www\.)?drom\.ru)?(/catalog/([a-z][a-z0-9_\-]+)/)"',
        html,
    ):
        full, slug = m.group(1), m.group(2)
        if slug in ("engine", "frame", "all"):
            continue
        out.append(BASE + full)
    # de-dup preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def list_models(session: requests.Session, brand_url: str) -> list[str]:
    """Return absolute model URLs like https://www.drom.ru/catalog/toyota/camry/."""
    html = fetch(session, brand_url) or ""
    brand_slug = brand_url.rstrip("/").rsplit("/", 1)[-1]
    out: list[str] = []
    pat = re.compile(rf'href="(https://www\.drom\.ru/catalog/{re.escape(brand_slug)}/[a-z][a-z0-9_\-]+/?)"')
    for m in pat.finditer(html):
        u = m.group(1)
        if u.endswith("/engine/") or u.endswith("/frame/"):
            continue
        if "/g_" in u or "/m_" in u:
            continue
        out.append(u.rstrip("/") + "/")
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def list_generations(session: requests.Session, model_url: str) -> list[str]:
    """Return absolute generation URLs like .../camry/g_201405_4270/."""
    html = fetch(session, model_url) or ""
    out: list[str] = []
    for m in re.finditer(r'href="(/catalog/[^"]+?/g_\d+(?:_\d+)?/)"', html):
        out.append(BASE + m.group(1))
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


# ---------------------------------------------------------------------------
# Spec extraction from a generation page
# ---------------------------------------------------------------------------


# Drom renders structured spec rows as
#   <span class="...">LABEL<!-- -->:</span><span class="..."> VALUE </span>
# (or sometimes with an inner <a>VALUE</a>). The CSS-in-JS class hash
# changes between deploys, so we match by structure.
_SPAN_PAIR = re.compile(
    r'<span[^>]*>\s*([^<>{}]{1,50}?)\s*(?:<!--\s*-->\s*)?:</span>\s*'
    r'<span[^>]*>(.*?)</span>',
    re.DOTALL,
)
_TAG_STRIP = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean(value: str) -> str:
    txt = _TAG_STRIP.sub(" ", value)
    txt = txt.replace("\xa0", " ").replace("&mdash;", "—")
    return _WS.sub(" ", txt).strip()


# Map drom labels to our DB columns.  Multiple aliases per column.
# NOTE: drom uses "Кузов" for the chassis frame code (e.g. ASV51), not a body
# style label. We deliberately ignore that field. The body type (sedan / SUV /
# etc.) on drom shows up in og:title meta-tags only and is parsed separately.
LABEL_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^класс\b", re.I),                "body_style"),
    (re.compile(r"^тип\s+кузова", re.I),           "body_style"),
    (re.compile(r"^привод\b", re.I),               "drive_type"),
    (re.compile(r"^кол(?:ичество)?\s+двер", re.I), "doors"),
    (re.compile(r"^двиг(ат|ат\.|атель)\b", re.I),  "engine"),
    (re.compile(r"объ[её]м\s+двиг", re.I),         "engine_displacement_cc"),
    (re.compile(r"^мощность\b", re.I),             "power_w"),
    (re.compile(r"^топливо\b", re.I),              "fuel_type"),
    (re.compile(r"^трансмиссия\b", re.I),          "transmission"),
    (re.compile(r"^коробка(\s+передач)?$", re.I),  "transmission"),
    (re.compile(r"^длина\b", re.I),                "length_mm"),
    (re.compile(r"^ширина\b", re.I),               "width_mm"),
    (re.compile(r"^высота\b", re.I),               "height_mm"),
    (re.compile(r"кол[её]сная\s+база", re.I),      "wheelbase_mm"),
    (re.compile(r"снаряж", re.I),                  "mass_kg"),
    (re.compile(r"^макс(имальная)?\.?\s*скорость", re.I), "max_speed"),
    (re.compile(r"начало\s+произв", re.I),         "production_start"),
    (re.compile(r"окончание\s+произв", re.I),      "discontinued"),
]


# Convert a free-form value string to the right column type.
_NUM = re.compile(r"(-?\d+(?:[.,]\d+)?)")


def _to_int(s: str) -> int | None:
    m = _NUM.search(s.replace(" ", ""))
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", ".")))
    except ValueError:
        return None


def _to_float(s: str) -> float | None:
    m = _NUM.search(s.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _to_mm(value: str) -> float | None:
    """Drom typically writes 'XXXX мм'. Accept loose variants."""
    v = value.replace("\xa0", " ").lower()
    m = re.search(r"(\d{3,5}(?:[.,]\d+)?)\s*мм", v)
    if m:
        return float(m.group(1).replace(",", "."))
    return _to_float(v) if 100 < (_to_float(v) or 0) < 10000 else None


def _to_kg(value: str) -> float | None:
    v = value.replace("\xa0", " ").lower()
    m = re.search(r"(\d{3,5}(?:[.,]\d+)?)\s*кг", v)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def _to_w(value: str) -> float | None:
    v = value.replace("\xa0", " ").lower()
    # 173 л.с. or 127 кВт
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*л\.?\s*с", v)
    if m:
        return float(m.group(1).replace(",", ".")) * 735.5
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*квт", v)
    if m:
        return float(m.group(1).replace(",", ".")) * 1000
    return None


def _to_cc(value: str) -> float | None:
    v = value.replace("\xa0", " ").lower()
    # 2.0 л / 2.5 л
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*л\b", v)
    if m:
        liters = float(m.group(1).replace(",", "."))
        if 0.3 < liters < 12:
            return round(liters * 1000, 1)
    # 1998 см³ / 2000 cc
    m = re.search(r"(\d{3,5})\s*(см³|см3|cc|cm³|cm3)", v)
    if m:
        return float(m.group(1))
    return None


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


# Prose-fallback regexes for fields that appear in editorial text rather than
# the structured spec block on some drom pages.
def _liters_to_cc_strict(s: str) -> float | None:
    try:
        liters = float(str(s).replace(",", "."))
    except ValueError:
        return None
    return round(liters * 1000, 1) if 0.3 < liters < 12 else None


PROSE_RULES: list[tuple[re.Pattern, str, callable]] = [
    (re.compile(r"кол[её]сная\s+база[^\d]{0,30}(\d{3,5})\s*мм", re.I), "wheelbase_mm", float),
    (re.compile(r"длина[^\d]{0,30}(\d{3,5})\s*мм", re.I),               "length_mm", float),
    (re.compile(r"ширина[^\d]{0,30}(\d{3,5})\s*мм", re.I),              "width_mm", float),
    (re.compile(r"высота[^\d]{0,30}(\d{3,5})\s*мм", re.I),              "height_mm", float),
    (re.compile(r"снаряж\w*\s+масс\w*[^\d]{0,30}(\d{3,5})\s*кг", re.I), "mass_kg", float),
    (re.compile(r"объ[её]м[^\d]{0,30}(\d+(?:[.,]\d+)?)\s*л\b", re.I),
     "engine_displacement_cc", _liters_to_cc_strict),
]


def parse_generation_specs(html: str) -> dict:
    """Pull every label/value pair we recognise from a /catalog/.../g_*/ page."""
    out: dict = {}

    # 1. structured spans
    for m in _SPAN_PAIR.finditer(html):
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
                # Only set if not already filled (first occurrence wins).
                out.setdefault(col, cooked)
                break

    # 2. prose fallbacks (only fill fields still missing)
    text_only = _clean(html)
    for pat, col, conv in PROSE_RULES:
        if col in out:
            continue
        m = pat.search(text_only)
        if m:
            try:
                out[col] = conv(m.group(1))
            except (ValueError, TypeError):
                pass
    return out


# ---------------------------------------------------------------------------
# DB layer (mirrors enrich_autodata.py behaviour)
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
        ("drom_url", "TEXT"),
        ("source_specs", "TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE cars ADD COLUMN {col} {kind}")
    conn.commit()


_NORM_RE = re.compile(r"[^a-z0-9а-я]+", re.IGNORECASE)


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    return _NORM_RE.sub("", s.lower())


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
        candidates: set[str] = {label}
        if manuf and label.lower().startswith(manuf.lower() + " "):
            candidates.add(label[len(manuf):].strip())
        no_paren = re.sub(r"\s*\([^)]*\)", "", label).strip()
        if no_paren:
            candidates.add(no_paren)
        row = ExistingRow(qid, label, manuf)
        for c in candidates:
            cn = _normalize(c)
            if cn:
                idx.setdefault(cn, row)
            if manuf:
                idx.setdefault(_normalize(manuf + c), row)
    return idx


def _match(idx: dict[str, ExistingRow], brand: str, model: str) -> ExistingRow | None:
    # Only match by full brand+model. Matching by model alone produces false
    # positives like "Audi A1" → "Toyota A1" because short model names like
    # "A1" / "A4" / "3" are non-unique across manufacturers.
    for key in (
        _normalize(brand + " " + model),
        _normalize(brand + model),
    ):
        if key and key in idx:
            row = idx[key]
            # Extra guard: confirm the matched row's manufacturer matches our
            # brand (case-insensitive substring), so we don't accept stale
            # collisions if the index ever happens to contain duplicates.
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
        cols = ["qid", "label", "manufacturer", "drom_url", "source_specs", "updated_at"] + list(_FILLABLE)
        vals: list = [qid, model, brand, url, "drom.ru", time.strftime("%Y-%m-%d %H:%M:%S")]
        for f in _FILLABLE:
            vals.append(specs.get(f))
        placeholders = ",".join(["?"] * len(cols))
        conn.execute(f"INSERT INTO cars ({','.join(cols)}) VALUES ({placeholders})", vals)
        return True

    _label, _manuf, cur_source_specs, *cur_vals = existing
    cur = dict(zip(_FILLABLE, cur_vals))
    sets: list[str] = ["drom_url=?"]
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
        params.append(("," if cur_source_specs else "") + "drom:" + ",".join(src_marks))
        sets.append("updated_at=datetime('now')")
        params.append(qid)
        conn.execute(f"UPDATE cars SET {', '.join(sets)} WHERE qid=?", params)
        return True

    conn.execute("UPDATE cars SET drom_url=? WHERE qid=?", (url, qid))
    return False


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


@dataclass
class GenTask:
    brand_slug: str
    brand_human: str
    model_slug: str
    model_human: str
    gen_url: str
    gen_id: str


@dataclass
class GenResult:
    task: GenTask
    specs: dict


def _slug_to_human(slug: str) -> str:
    return slug.replace("_", " ").replace("-", " ").title()


def _process_gen(session: requests.Session, task: GenTask) -> GenResult | None:
    html = fetch(session, task.gen_url)
    if not html:
        return None
    specs = parse_generation_specs(html)
    if not specs:
        return None
    return GenResult(task, specs)


def crawl(
    db_path: Path,
    brand_limit: int = 0,
    model_limit: int = 0,
    workers: int = 6,
    resume: bool = True,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_columns(conn)
    idx = _build_match_index(conn)
    log.info("matched index: %d entries", len(idx))

    bootstrap = requests.Session()
    brands = list_brands(bootstrap)
    log.info("brands found: %d", len(brands))
    if brand_limit:
        brands = brands[:brand_limit]

    tls = threading.local()

    def _session() -> requests.Session:
        s = getattr(tls, "session", None)
        if s is None:
            s = requests.Session()
            tls.session = s
        return s

    # Step 1: enumerate models per brand (parallel)
    log.info("listing models...")
    model_tasks: list[tuple[str, str, str, str]] = []  # (brand_slug, brand_human, model_slug, model_url)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def _list(burl: str) -> list[tuple[str, str, str, str]]:
            slug = burl.rstrip("/").rsplit("/", 1)[-1]
            human = _slug_to_human(slug)
            models = list_models(_session(), burl)
            if model_limit:
                models = models[:model_limit]
            out = []
            for m in models:
                m_slug = m.rstrip("/").rsplit("/", 1)[-1]
                out.append((slug, human, m_slug, m))
            return out
        for fut in tqdm([pool.submit(_list, b) for b in brands], desc="brands"):
            model_tasks.extend(fut.result())
    log.info("models discovered: %d", len(model_tasks))

    # Step 2: for each model, list generations and pick the most recent one
    log.info("listing generations for %d models...", len(model_tasks))
    gen_tasks: list[GenTask] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def _gens(t):
            bs, bh, ms, murl = t
            gens = list_generations(_session(), murl)
            if not gens:
                return None
            # Sort generations by the YYYYMM prefix encoded in the slug
            # (e.g. g_201405_4270) so the most recent generation is first.
            def _gen_key(u: str) -> int:
                m = re.search(r"/g_(\d{6})_\d+/?$", u)
                return int(m.group(1)) if m else 0
            gens.sort(key=_gen_key, reverse=True)
            gen_url = gens[0]
            gid = gen_url.rstrip("/").rsplit("g_", 1)[-1]
            return GenTask(bs, bh, ms, _slug_to_human(ms), gen_url, gid)
        for fut in tqdm([pool.submit(_gens, t) for t in model_tasks], desc="generations"):
            r = fut.result()
            if r:
                gen_tasks.append(r)
    log.info("generations to crawl: %d", len(gen_tasks))

    seen_urls: set[str] = set()
    if resume:
        seen_urls = {row[0] for row in conn.execute("SELECT drom_url FROM cars WHERE drom_url IS NOT NULL")}

    inserted = 0
    updated = 0
    db_lock = threading.Lock()

    log.info("crawling specs (workers=%d)...", workers)

    def _spec_worker(t: GenTask) -> GenResult | None:
        return _process_gen(_session(), t)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_spec_worker, t) for t in gen_tasks]
        for fut in tqdm(futures, desc="specs", total=len(futures)):
            try:
                result = fut.result()
            except Exception as e:
                log.debug("spec task failed: %r", e)
                continue
            if not result:
                continue
            if result.task.gen_url in seen_urls:
                continue
            existing = _match(idx, result.task.brand_human, result.task.model_human)
            qid = (
                existing.qid
                if existing
                else f"drom:{result.task.brand_slug}-{result.task.model_slug}-{result.task.gen_id}"
            )
            with db_lock:
                try:
                    changed = upsert(
                        conn,
                        qid,
                        result.task.brand_human,
                        result.task.model_human,
                        result.specs,
                        result.task.gen_url,
                    )
                    if changed:
                        if existing:
                            updated += 1
                        else:
                            inserted += 1
                            idx[_normalize(result.task.brand_human + result.task.model_human)] = ExistingRow(
                                qid, result.task.model_human, result.task.brand_human
                            )
                except sqlite3.Error as e:
                    log.warning("DB error %r", e)
                if (inserted + updated) % 200 == 0:
                    conn.commit()

    with db_lock:
        conn.commit()
    conn.close()
    log.info("done: inserted %d new rows, updated %d existing", inserted, updated)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).parent / "data" / "cars.db"))
    ap.add_argument("--brand-limit", type=int, default=0)
    ap.add_argument("--model-limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()
    crawl(
        Path(args.db),
        brand_limit=args.brand_limit,
        model_limit=args.model_limit,
        workers=args.workers,
        resume=not args.no_resume,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
