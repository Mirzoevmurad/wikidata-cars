#!/usr/bin/env python3
"""
enrich_naavtotrasse.py — scraper for naavtotrasse.ru/cat/.

The site exposes a four-level catalog:
    /cat/                                 – brands
    /cat/<brand>/                         – models
    /cat/<brand>/<model>/                 – variants (one page per
                                            generation × body × market),
                                            with H1 like
                                              "Audi A4 08.2007 - 010.2011 37 Универсал"
                                            so we can read the year range and
                                            body style straight off the page
    /cat/<brand>/<model>/<year>_<id>/     – the variant page itself, with a
                                            big <table> of trims (engine cc /
                                            drive / transmission / max speed /
                                            fuel consumption per row).

What this scraper does:

1. For every model, list all variant URLs and read the H1 of each one to
   collect (year_start, year_end, body) triples → write them into a new
   ``generations`` table for the timeline UI.
2. For each variant, parse the trim table and aggregate "best-effort" model
   specs (latest variant's body_style + most common engine displacement,
   drive_type, transmission, max_speed) and back-fill the matching ``cars``
   row, exactly like ``enrich_drom.py`` does — only filling empty cells.

Cloudflare blocks the site for many cloud IPs but not for ours (Frankfurt
VPS).
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import requests
from tqdm import tqdm

BASE = "https://naavtotrasse.ru"
CATALOG = f"{BASE}/cat/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.7",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("naavtotrasse")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def fetch(session: requests.Session, url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
            log.debug("http %s for %s", r.status_code, url)
        except requests.RequestException as e:
            log.debug("net %r for %s", e, url)
        time.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------


def list_brands(session: requests.Session) -> list[str]:
    html = fetch(session, CATALOG) or ""
    out: list[str] = []
    for m in re.finditer(r'href=[\'"](/cat/([a-z][a-z0-9_\-]+)/)[\'"]', html):
        slug = m.group(2)
        if slug in ("cat", ""):
            continue
        out.append(BASE + m.group(1))
    return sorted(set(out))


def list_models(session: requests.Session, brand_url: str) -> list[str]:
    html = fetch(session, brand_url) or ""
    slug = brand_url.rstrip("/").rsplit("/", 1)[-1]
    out: list[str] = []
    pat = rf'href=[\'"](/cat/{re.escape(slug)}/([a-z0-9][a-z0-9_\-]*)/)[\'"]'
    for m in re.finditer(pat, html):
        out.append(BASE + m.group(1))
    return sorted(set(out))


_VARIANT_LINK_RE = re.compile(
    r'<a[^>]*href=[\'"](/cat/[^/]+/[^/]+/(\d{4})_(\d+)/)[\'"][^>]*>(.*?)</a>',
    re.S,
)


@dataclass
class Variant:
    url: str
    year_start: int
    year_end: int | None  # None means "present"
    body: str
    page_id: int


def list_variants(session: requests.Session, model_url: str) -> list[Variant]:
    """Read the model page, find every /cat/.../<YYYY>_<id>/ link and parse the
    surrounding text 'MM.YYYY - MM.YYYY <id> <body>' to learn each variant's
    timeline."""
    html = fetch(session, model_url) or ""
    out: list[Variant] = []
    seen: set[str] = set()
    for m in _VARIANT_LINK_RE.finditer(html):
        href, _yr, pid_s, inner = m.groups()
        if href in seen:
            continue
        seen.add(href)
        text = re.sub(r"<[^>]+>", " ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        # naavtotrasse occasionally types '010' as a typo for '10', so we
        # accept 1-3 digit months.
        m2 = re.search(
            r"(\d{1,3})\.(\d{4})\s*-\s*(\d{1,3}\.\d{4}|н\.в\.)",
            text,
        )
        if not m2:
            continue
        year_start = int(m2.group(2))
        end_token = m2.group(3)
        if end_token.startswith("н"):
            year_end = None
        else:
            year_end = int(end_token.split(".")[1])
        body_match = re.search(
            r"(Седан|Универсал|Хэтчбек|Хетчбек|Лифтбек|Купе|Кабриолет|Открытый кузов|Внедорожник|Кроссовер|Минивэн|Пикап|Микроавтобус|Фургон|Родстер|Тарга)",
            text,
            re.I,
        )
        body = body_match.group(1) if body_match else ""
        out.append(
            Variant(
                url=BASE + href,
                year_start=year_start,
                year_end=year_end,
                body=body,
                page_id=int(pid_s),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Variant page parsing — pull the trim table and aggregate
# ---------------------------------------------------------------------------


_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


@dataclass
class TrimRow:
    name: str
    drive: str = ""
    transmission: str = ""
    engine_cc: int | None = None
    max_speed_kmh: int | None = None


def parse_trim_table(html: str) -> list[TrimRow]:
    """Find the first <table> with the standard naavtotrasse trim layout
    (header includes 'Комплектация'/'Привод'/'Двигатель') and parse rows."""
    out: list[TrimRow] = []
    for tbl_html in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        rows = _TR_RE.findall(tbl_html)
        if len(rows) < 2:
            continue
        header_cells = [_clean(c) for c in _CELL_RE.findall(rows[0])]
        if not any("омплектац" in h for h in header_cells):
            continue
        # Column index map.
        idx = {}
        for i, h in enumerate(header_cells):
            hl = h.lower()
            if "омплектац" in hl:
                idx["name"] = i
            elif "ривод" in hl:
                idx["drive"] = i
            elif "рансмисси" in hl:
                idx["transmission"] = i
            elif "двигат" in hl and ("куб" in hl or "см" in hl):
                idx["engine_cc"] = i
            elif "скорост" in hl:
                idx["max_speed_kmh"] = i
        for r in rows[1:]:
            cells = [_clean(c) for c in _CELL_RE.findall(r)]
            if len(cells) < 2:
                continue
            tr = TrimRow(name=cells[idx.get("name", 1)] if "name" in idx else cells[1])
            if "drive" in idx and idx["drive"] < len(cells):
                tr.drive = cells[idx["drive"]]
            if "transmission" in idx and idx["transmission"] < len(cells):
                tr.transmission = cells[idx["transmission"]]
            if "engine_cc" in idx and idx["engine_cc"] < len(cells):
                m = re.search(r"\d+", cells[idx["engine_cc"]])
                if m:
                    tr.engine_cc = int(m.group(0))
            if "max_speed_kmh" in idx and idx["max_speed_kmh"] < len(cells):
                m = re.search(r"\d+", cells[idx["max_speed_kmh"]])
                if m:
                    tr.max_speed_kmh = int(m.group(0))
            out.append(tr)
        return out  # only the first matching table
    return out


def aggregate_specs(trims: list[TrimRow], body: str) -> dict:
    """Collapse a trim table into a single specs dict for cars.* upsert."""
    if not trims:
        return {}
    drives = Counter(t.drive for t in trims if t.drive)
    transmissions = Counter(t.transmission for t in trims if t.transmission)
    ccs = [t.engine_cc for t in trims if t.engine_cc]
    speeds = [t.max_speed_kmh for t in trims if t.max_speed_kmh]
    out: dict = {}
    if drives:
        out["drive_type"] = drives.most_common(1)[0][0]
    if transmissions:
        out["transmission"] = transmissions.most_common(1)[0][0]
    if ccs:
        out["engine_displacement_cc"] = max(ccs)  # peak displacement
    if speeds:
        out["max_speed"] = max(speeds)
    if body:
        out["body_style"] = body
    return out


# ---------------------------------------------------------------------------
# DB integration
# ---------------------------------------------------------------------------


_FILLABLE = (
    "engine_displacement_cc",
    "drive_type",
    "transmission",
    "max_speed",
    "body_style",
    "production_start",
    "discontinued",
)


def ensure_columns_and_table(conn: sqlite3.Connection) -> None:
    have = {row[1] for row in conn.execute("PRAGMA table_info(cars)").fetchall()}
    for col, kind in (
        ("naavtotrasse_url", "TEXT"),
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


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class ModelTask:
    brand_slug: str
    brand_human: str
    model_slug: str
    model_human: str
    model_url: str


@dataclass
class ModelResult:
    task: ModelTask
    variants: list[Variant] = field(default_factory=list)
    specs: dict = field(default_factory=dict)


def _process_model(session: requests.Session, t: ModelTask) -> ModelResult | None:
    variants = list_variants(session, t.model_url)
    if not variants:
        return None
    # Pick the most recent variant (greatest year_start, "present" wins)
    def _key(v: Variant):
        return (v.year_start, 0 if v.year_end is None else 1, v.year_end or 0)
    latest = max(variants, key=_key)
    html = fetch(session, latest.url)
    specs = {}
    if html:
        trims = parse_trim_table(html)
        specs = aggregate_specs(trims, latest.body)
    if latest.year_start:
        specs.setdefault("production_start", str(latest.year_start))
    if latest.year_end:
        specs.setdefault("discontinued", str(latest.year_end))
    return ModelResult(task=t, variants=variants, specs=specs)


def _upsert(
    conn: sqlite3.Connection,
    qid: str,
    brand: str,
    model: str,
    specs: dict,
    url: str,
) -> tuple[bool, bool]:
    """Returns (inserted, updated_any_field)."""
    existing = conn.execute(
        f"SELECT label, manufacturer, source_specs, {', '.join(_FILLABLE)} FROM cars WHERE qid=?",
        (qid,),
    ).fetchone()
    if existing is None:
        cols = (
            ["qid", "label", "manufacturer", "naavtotrasse_url", "source_specs", "updated_at"]
            + list(_FILLABLE)
        )
        vals: list = [
            qid,
            model,
            brand,
            url,
            "naavtotrasse",
            time.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        for f in _FILLABLE:
            vals.append(specs.get(f))
        placeholders = ",".join(["?"] * len(cols))
        conn.execute(
            f"INSERT INTO cars ({','.join(cols)}) VALUES ({placeholders})", vals
        )
        return True, True

    _label, _manuf, cur_source_specs, *cur_vals = existing
    cur = dict(zip(_FILLABLE, cur_vals))
    sets: list[str] = ["naavtotrasse_url=?"]
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
        sets.append("source_specs = COALESCE(NULLIF(source_specs,''),'') || ?")
        params.append(("," if cur_source_specs else "") + "naavtotrasse:" + ",".join(src_marks))
        sets.append("updated_at=datetime('now')")
        params.append(qid)
        conn.execute(f"UPDATE cars SET {', '.join(sets)} WHERE qid=?", params)
        return False, True
    conn.execute("UPDATE cars SET naavtotrasse_url=? WHERE qid=?", (url, qid))
    return False, False


def _store_generations(
    conn: sqlite3.Connection,
    qid: str,
    variants: list[Variant],
) -> int:
    """Dedupe variants by (year_start, year_end, body) — the same generation
    often spans multiple market/body listings — and insert one row per unique
    triple. Returns inserted-row count."""
    seen: dict[tuple[int, int | None, str], Variant] = {}
    for v in variants:
        key = (v.year_start, v.year_end, v.body or "")
        if key not in seen or v.page_id < seen[key].page_id:
            seen[key] = v
    ordered = sorted(
        seen.values(),
        key=lambda v: (v.year_start, 0 if v.year_end is None else 1, v.year_end or 0),
    )
    n = 0
    for i, v in enumerate(ordered, start=1):
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO generations
                    (qid, gen_index, year_start, year_end, body_style,
                     source, source_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (qid, i, v.year_start, v.year_end, v.body or None,
                 "naavtotrasse", v.url),
            )
            n += conn.total_changes and 1 or 0
        except sqlite3.Error as e:
            log.debug("gen insert %r: %r", v.url, e)
    return n


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
    model_tasks: list[ModelTask] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def _list(burl: str) -> list[ModelTask]:
            slug = burl.rstrip("/").rsplit("/", 1)[-1]
            human = _slug_to_human(slug)
            models = list_models(_session(), burl)
            if model_limit:
                models = models[:model_limit]
            out = []
            for m in models:
                m_slug = m.rstrip("/").rsplit("/", 1)[-1]
                out.append(ModelTask(slug, human, m_slug, _slug_to_human(m_slug), m))
            return out
        for fut in tqdm([pool.submit(_list, b) for b in brands], desc="brands"):
            model_tasks.extend(fut.result())
    log.info("models discovered: %d", len(model_tasks))

    inserted = 0
    updated = 0
    gens_added = 0
    db_lock = threading.Lock()

    log.info("crawling variants for %d models (workers=%d)...", len(model_tasks), workers)

    def _worker(t: ModelTask) -> ModelResult | None:
        try:
            return _process_model(_session(), t)
        except Exception as e:
            log.debug("model %s: %r", t.model_url, e)
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker, t) for t in model_tasks]
        for fut in tqdm(futures, desc="models", total=len(futures)):
            try:
                result = fut.result()
            except Exception as e:
                log.debug("worker: %r", e)
                continue
            if not result or not result.variants:
                continue
            existing = _match(idx, result.task.brand_human, result.task.model_human)
            if existing is not None:
                qid = existing.qid
            else:
                # synthetic id of the form naavtotrasse:<brand>-<model>
                qid = f"naavtotrasse:{result.task.brand_slug}-{result.task.model_slug}"
            latest_url = max(
                result.variants,
                key=lambda v: (v.year_start, 0 if v.year_end is None else 1, v.year_end or 0),
            ).url
            with db_lock:
                try:
                    is_new, changed = _upsert(
                        conn,
                        qid,
                        result.task.brand_human,
                        result.task.model_human,
                        result.specs,
                        latest_url,
                    )
                    if is_new:
                        inserted += 1
                        idx[_normalize(result.task.brand_human + result.task.model_human)] = ExistingRow(
                            qid, result.task.model_human, result.task.brand_human
                        )
                    elif changed:
                        updated += 1
                    gens_added += _store_generations(conn, qid, result.variants)
                except sqlite3.Error as e:
                    log.warning("DB error %r", e)
                if (inserted + updated) % 200 == 0:
                    conn.commit()

    with db_lock:
        conn.commit()
    conn.close()
    log.info(
        "done: inserted %d new rows, updated %d existing, %d generations stored",
        inserted, updated, gens_added,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="naavtotrasse.ru catalog enricher")
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
