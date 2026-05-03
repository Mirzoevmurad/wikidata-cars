#!/usr/bin/env python3
"""
enrich_chinamobil.py — minimal scraper for chinamobil.ru.

The site is editorial-first and exposes very few structured fields per model;
we only pull what is reliably present:

* one logo / hero image  (``/photo/<ModelDir>/logo2.jpg``)
* an inline 'Двигатели: <list>' line that we surface as the ``engine`` field
* a 'Поколения' navigation block that lists older year-anchored model pages,
  which we treat as generation rows (year_start = link year, body NULL).

Catalog walk:
    /catalog.php                          – marks (brands)
    /<brand-slug>/                        – model list
    /<brand-slug>/<model-slug>/           – model page (the only spec source)
    /<brand-slug>/<model-slug>/<YYYY>/    – older generation page

The model is matched against existing ``cars`` rows with the same case-
insensitive (brand+model) key as the other enrichers, and only fills empty
columns. Models we don't have are inserted with a synthetic
``chinamobil:<brand-slug>-<model-slug>`` qid — useful because most of the
Chinese makes (Chery, Geely, Haval, BYD, JAC, FAW, GAC, Changan, Dongfeng,
JETOUR, EXEED, Tank, Voyah, Hongqi, ...) have models that never make it into
Wikidata.
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

BASE = "https://www.chinamobil.ru"
CATALOG = f"{BASE}/catalog.php"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.7",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("chinamobil")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def fetch(session: requests.Session, url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                # chinamobil serves utf-8 declared in <meta>
                return r.content.decode("utf-8", errors="replace")
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------


_BRAND_DENY = {
    "catalog.php", "wiki", "parts", "map", "cars", "news", "sales",
    "allopinion.php", "allpress.php", "allphotos.php", "allvideo.php",
    "alldoc.php", "dealers.php", "modellist.php", "feedback", "search",
}


def list_brands(session: requests.Session) -> list[str]:
    html = fetch(session, CATALOG) or ""
    out: list[str] = []
    for m in re.finditer(r'href=[\'"](/([a-z][a-z0-9-]*)/)[\'"]', html):
        slug = m.group(2)
        if slug in _BRAND_DENY:
            continue
        out.append(BASE + m.group(1))
    return sorted(set(out))


def list_models(session: requests.Session, brand_url: str) -> list[str]:
    html = fetch(session, brand_url) or ""
    slug = brand_url.rstrip("/").rsplit("/", 1)[-1]
    out: list[str] = []
    pat = rf'href=[\'"](/{re.escape(slug)}/([a-z0-9][a-z0-9_\-]+)/)[\'"]'
    for m in re.finditer(pat, html):
        m_slug = m.group(2)
        # Older-generation links use a year as the second segment; we want
        # the canonical /<brand>/<model>/ entry.
        if re.fullmatch(r"\d{4}", m_slug):
            continue
        out.append(BASE + m.group(1))
    return sorted(set(out))


@dataclass
class ModelInfo:
    brand_slug: str
    brand_human: str
    model_slug: str
    model_human: str
    url: str
    image_url: str | None = None
    engine: str | None = None
    generations: list[tuple[int, str]] | None = None  # (year, source_url)


def _h1_to_brand_model(h1: str, fallback_brand: str, fallback_model: str) -> tuple[str, str]:
    """H1 is usually 'Chery Tiggo 8'. We split into brand + model on the first
    word match against fallback_brand."""
    h1 = h1.strip()
    if h1.lower().startswith(fallback_brand.lower()):
        return fallback_brand, h1[len(fallback_brand):].strip() or fallback_model
    return fallback_brand, h1 or fallback_model


def parse_model_page(html: str, brand_human: str, model_human: str, model_url: str) -> dict:
    out: dict = {}
    # H1 → brand + model
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if m:
        h1 = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
        if h1:
            br, mo = _h1_to_brand_model(h1, brand_human, model_human)
            out["brand_human"] = br
            out["model_human"] = mo

    # Hero image (logo2.jpg lives next to /photo/<ModelDir>/)
    img = re.search(r"src=['\"](/photo/[A-Za-z0-9_]+/logo2\.[a-z]+)['\"]", html)
    if img:
        out["image_url"] = BASE + img.group(1)

    # Engines line
    eng = re.search(r"Двигатели:\s*([^<\n]{1,300})", html)
    if eng:
        out["engine"] = eng.group(1).strip().rstrip(",;")

    # Generations: 'Поколения' block contains <a href='/<brand>/<model>/<YYYY>/'>YYYY</a>
    gens: list[tuple[int, str]] = []
    idx = html.find("Поколения")
    if idx > 0:
        section = html[idx : idx + 4000]
        # current page is bold
        for ym in re.finditer(r"<b>(\d{4})</b>", section):
            gens.append((int(ym.group(1)), model_url))
        for hm in re.finditer(
            r"href=['\"](/[^'\"]+/(\d{4})/)['\"]", section
        ):
            gens.append((int(hm.group(2)), BASE + hm.group(1)))
    if gens:
        # dedupe on year, keep earliest URL
        seen: dict[int, str] = {}
        for y, u in gens:
            seen.setdefault(y, u)
        out["generations"] = sorted(seen.items())
    return out


# ---------------------------------------------------------------------------
# DB integration
# ---------------------------------------------------------------------------


_FILLABLE = ("engine", "image_url")


def ensure_columns_and_table(conn: sqlite3.Connection) -> None:
    have = {row[1] for row in conn.execute("PRAGMA table_info(cars)").fetchall()}
    for col, kind in (
        ("chinamobil_url", "TEXT"),
        ("source_specs", "TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE cars ADD COLUMN {col} {kind}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qid TEXT NOT NULL,
            gen_index INTEGER,
            year_start INTEGER,
            year_end INTEGER,
            body_style TEXT,
            source TEXT,
            source_url TEXT,
            UNIQUE(qid, source, source_url)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_generations_qid ON generations(qid)")
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
    for key in (
        _normalize(brand + " " + model),
        _normalize(brand + model),
    ):
        if key and key in idx:
            row = idx[key]
            if (
                not row.manufacturer
                or _normalize(row.manufacturer) == _normalize(brand)
                or _normalize(brand) in _normalize(row.manufacturer)
                or _normalize(row.manufacturer) in _normalize(brand)
            ):
                return row
    return None


def _slug_to_human(slug: str) -> str:
    return slug.replace("_", " ").replace("-", " ").title()


def _upsert(
    conn: sqlite3.Connection,
    qid: str,
    brand: str,
    model: str,
    info: dict,
    url: str,
) -> tuple[bool, bool]:
    existing = conn.execute(
        f"SELECT label, manufacturer, source_specs, {', '.join(_FILLABLE)} FROM cars WHERE qid=?",
        (qid,),
    ).fetchone()
    if existing is None:
        cols = (
            ["qid", "label", "manufacturer", "chinamobil_url", "source_specs", "updated_at"]
            + list(_FILLABLE)
        )
        vals: list = [
            qid,
            model,
            brand,
            url,
            "chinamobil",
            time.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        for f in _FILLABLE:
            vals.append(info.get(f))
        placeholders = ",".join(["?"] * len(cols))
        conn.execute(
            f"INSERT INTO cars ({','.join(cols)}) VALUES ({placeholders})", vals
        )
        return True, True

    _label, _manuf, cur_source_specs, *cur_vals = existing
    cur = dict(zip(_FILLABLE, cur_vals))
    sets: list[str] = ["chinamobil_url=?"]
    params: list = [url]
    src_marks: list[str] = []
    for f in _FILLABLE:
        new_val = info.get(f)
        if new_val in (None, "", 0):
            continue
        if cur.get(f) in (None, "", 0):
            sets.append(f"{f}=?")
            params.append(new_val)
            src_marks.append(f)
    if len(sets) > 1:
        sets.append("source_specs = COALESCE(NULLIF(source_specs,''),'') || ?")
        params.append(("," if cur_source_specs else "") + "chinamobil:" + ",".join(src_marks))
        sets.append("updated_at=datetime('now')")
        params.append(qid)
        conn.execute(f"UPDATE cars SET {', '.join(sets)} WHERE qid=?", params)
        return False, True
    conn.execute("UPDATE cars SET chinamobil_url=? WHERE qid=?", (url, qid))
    return False, False


def _store_generations(
    conn: sqlite3.Connection,
    qid: str,
    gens: list[tuple[int, str]],
) -> int:
    if not gens:
        return 0
    ordered = sorted(gens, key=lambda yu: yu[0])
    n = 0
    for i, (year, url) in enumerate(ordered, start=1):
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO generations
                    (qid, gen_index, year_start, year_end, body_style,
                     source, source_url)
                VALUES (?, ?, ?, NULL, NULL, ?, ?)
                """,
                (qid, i, year, "chinamobil", url),
            )
            n += conn.total_changes and 1 or 0
        except sqlite3.Error:
            pass
    return n


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def crawl(
    db_path: Path,
    brand_limit: int = 0,
    model_limit: int = 0,
    workers: int = 6,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_columns_and_table(conn)
    idx = _build_match_index(conn)
    log.info("matched index: %d entries", len(idx))

    bootstrap = requests.Session()
    brands = list_brands(bootstrap)
    log.info("brands: %d", len(brands))
    if brand_limit:
        brands = brands[:brand_limit]

    tls = threading.local()

    def _session() -> requests.Session:
        s = getattr(tls, "session", None)
        if s is None:
            s = requests.Session()
            tls.session = s
        return s

    log.info("listing models...")
    model_tasks: list[ModelInfo] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def _list(burl: str) -> list[ModelInfo]:
            slug = burl.rstrip("/").rsplit("/", 1)[-1]
            human = _slug_to_human(slug)
            models = list_models(_session(), burl)
            if model_limit:
                models = models[:model_limit]
            out = []
            for m in models:
                m_slug = m.rstrip("/").rsplit("/", 1)[-1]
                out.append(ModelInfo(slug, human, m_slug, _slug_to_human(m_slug), m))
            return out
        for fut in tqdm([pool.submit(_list, b) for b in brands], desc="brands"):
            model_tasks.extend(fut.result())
    log.info("models: %d", len(model_tasks))

    inserted = updated = gens_added = 0
    db_lock = threading.Lock()

    def _worker(t: ModelInfo) -> ModelInfo | None:
        html = fetch(_session(), t.url)
        if not html:
            return None
        info = parse_model_page(html, t.brand_human, t.model_human, t.url)
        if not info:
            return None
        t.image_url = info.get("image_url")
        t.engine = info.get("engine")
        t.generations = info.get("generations")
        if "brand_human" in info:
            t.brand_human = info["brand_human"]
        if "model_human" in info:
            t.model_human = info["model_human"]
        return t

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker, t) for t in model_tasks]
        for fut in tqdm(futures, desc="models", total=len(futures)):
            try:
                t = fut.result()
            except Exception:
                continue
            if not t:
                continue
            existing = _match(idx, t.brand_human, t.model_human)
            qid = existing.qid if existing else f"chinamobil:{t.brand_slug}-{t.model_slug}"
            info = {"engine": t.engine, "image_url": t.image_url}
            with db_lock:
                try:
                    is_new, changed = _upsert(conn, qid, t.brand_human, t.model_human, info, t.url)
                    if is_new:
                        inserted += 1
                        idx[_normalize(t.brand_human + t.model_human)] = ExistingRow(
                            qid, t.model_human, t.brand_human
                        )
                    elif changed:
                        updated += 1
                    if t.generations:
                        gens_added += _store_generations(conn, qid, t.generations)
                except sqlite3.Error as e:
                    log.warning("DB error %r", e)
                if (inserted + updated) % 200 == 0:
                    conn.commit()

    with db_lock:
        conn.commit()
    conn.close()
    log.info(
        "done: inserted %d, updated %d, %d generations stored",
        inserted, updated, gens_added,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="chinamobil.ru catalog enricher")
    ap.add_argument("--db", default="data/cars.db")
    ap.add_argument("--brand-limit", type=int, default=0)
    ap.add_argument("--model-limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    crawl(
        Path(args.db),
        brand_limit=args.brand_limit,
        model_limit=args.model_limit,
        workers=args.workers,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
