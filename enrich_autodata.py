#!/usr/bin/env python3
"""
enrich_autodata.py — auxiliary scraper for auto-data.net.

Walks the brand → model → generation → trim hierarchy and extracts ~63 spec
fields per trim. Aggregates per model (the latest generation, first trim) and
upserts into the same `cars` table used by the Wikidata pipeline.

For models that already exist in `cars` (matched by fuzzy
manufacturer+label), we only fill empty fields. Models that auto-data.net
has but Wikidata doesn't are inserted with a synthetic identifier
``ad:<model_id>`` so they stay distinct from real Wikidata QIDs.

Usage:
    python enrich_autodata.py --db data/cars.db
    python enrich_autodata.py --brand-limit 5 --model-limit 3   # smoke test
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
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

BASE = "https://www.auto-data.net"
ALLBRANDS = f"{BASE}/en/allbrands"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("autodata")


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def fetch(session: requests.Session, url: str, retries: int = 4) -> str | None:
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
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


_RE_HREF = re.compile(r'href="(/en/[^"]+)"')


def list_brands(session: requests.Session) -> list[str]:
    html = fetch(session, ALLBRANDS) or ""
    out: list[str] = []
    for m in _RE_HREF.findall(html):
        if "-brand-" in m and "/en/allbrands" not in m:
            out.append(m)
    # de-dup preserving order
    seen: set[str] = set()
    uniq = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def list_models(session: requests.Session, brand_url: str) -> list[str]:
    html = fetch(session, BASE + brand_url) or ""
    out: list[str] = []
    for m in _RE_HREF.findall(html):
        if "-model-" in m:
            out.append(m)
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def list_generations(session: requests.Session, model_url: str) -> list[str]:
    html = fetch(session, BASE + model_url) or ""
    out: list[str] = []
    for m in _RE_HREF.findall(html):
        if "-generation-" in m:
            out.append(m)
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def list_trims(session: requests.Session, gen_url: str) -> list[str]:
    """Return trim spec page URLs for one generation page (URLs end in ``-<NN>hp-<id>``)."""
    html = fetch(session, BASE + gen_url) or ""
    out: list[str] = []
    for m in _RE_HREF.findall(html):
        # trim URLs end with -<NNNhp>-<numeric_id>; example:
        #   /en/toyota-camry-ix-xv80-2.0-173hp-direct-shift-cvt-51624
        if re.search(r"hp-[a-z0-9\-]*-\d+$", m):
            out.append(m)
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


# ---------------------------------------------------------------------------
# Trim spec extraction
# ---------------------------------------------------------------------------


# Mapping from auto-data.net spec keys to our DB columns. Keys are matched by
# substring (case-insensitive), so we list distinguishing tokens.
SPEC_RULES: list[tuple[str, str]] = [
    # (substring, target_column)
    ("Length", "length_mm"),
    ("Width", "width_mm"),
    ("Height", "height_mm"),
    ("Wheelbase", "wheelbase_mm"),
    ("Kerb Weight", "mass_kg"),
    ("Curb weight", "mass_kg"),
    ("Brand", "_brand"),
    ("Model", "_model"),
    ("Generation", "_generation"),
    ("Modification (Engine)", "_modification"),
    ("Body type", "body_style"),
    ("Doors", "doors"),
    ("Fuel Type", "fuel_type"),
    ("Engine displacement", "engine_displacement_cc"),
    ("Power", "power_w"),
    ("Maximum speed", "max_speed"),
    ("Drive wheel", "drive_type"),
    ("Powertrain Architecture", "_powertrain"),
    ("Number of gears", "transmission"),
    ("Start of production", "production_start"),
    ("End of production", "discontinued"),
    ("Engine Model/Code", "engine"),
]


_NUM_UNIT = re.compile(r"([\d,]+(?:\.\d+)?)\s*(mm|cm|m|kg|kW|hp|Hp|HP|cm\s*3|cm³|L|km/h|in)\b")


def _to_mm(value: float, unit: str) -> float | None:
    u = unit.lower()
    if u == "mm":
        return value
    if u == "cm":
        return value * 10
    if u == "m":
        return value * 1000
    if u == "in":
        return value * 25.4
    return None


def _to_kg(value: float, unit: str) -> float | None:
    return value if unit.lower() == "kg" else None


def _to_w(value: float, unit: str) -> float | None:
    u = unit.lower()
    if u == "kw":
        return value * 1000
    if u in ("hp",):
        return value * 745.7
    return None


def _to_cc(value: float, unit: str) -> float | None:
    u = unit.lower().replace(" ", "")
    if u in ("cm3", "cm³"):
        return value
    if u == "l":
        return value * 1000
    return None


def _first_unit(text: str, conv) -> float | None:
    text = text.replace("\u00a0", " ").replace(",", "")
    m = _NUM_UNIT.search(text)
    if not m:
        return None
    try:
        return conv(float(m.group(1)), m.group(2))
    except (ValueError, TypeError):
        return None


def parse_trim_specs(html: str) -> dict:
    """Pick relevant fields out of an auto-data.net trim page table."""
    soup = BeautifulSoup(html, "lxml")
    raw: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) >= 2:
            k = cells[0].get_text(" ", strip=True)
            v = cells[1].get_text(" ", strip=True)
            if k and v and len(k) < 200:
                raw.setdefault(k, v)

    out: dict = {}
    for needle, col in SPEC_RULES:
        for k, v in raw.items():
            if needle.lower() in k.lower():
                if col == "length_mm" or col == "width_mm" or col == "height_mm" or col == "wheelbase_mm":
                    val = _first_unit(v, _to_mm)
                    if val and 100 < val < 100_000:
                        out[col] = round(val, 1)
                elif col == "mass_kg":
                    val = _first_unit(v, _to_kg)
                    if val and 100 < val < 50_000:
                        out[col] = round(val, 1)
                elif col == "power_w":
                    val = _first_unit(v, _to_w)
                    if val and 1_000 < val < 5_000_000:
                        out[col] = round(val, 1)
                elif col == "engine_displacement_cc":
                    val = _first_unit(v, _to_cc)
                    if val and 50 < val < 20_000:
                        out[col] = round(val, 1)
                elif col == "max_speed":
                    m = re.match(r"(\d+)\s*km/h", v)
                    if m:
                        out[col] = int(m.group(1))
                elif col == "doors":
                    m = re.search(r"\d+", v)
                    if m:
                        try:
                            out[col] = int(m.group(0))
                        except ValueError:
                            pass
                elif col == "production_start":
                    out[col] = v[:60]
                elif col == "discontinued":
                    out[col] = v[:60]
                elif col.startswith("_"):
                    out[col] = v.strip()
                else:
                    out[col] = v.split("[")[0].strip()[:300]
                break
    return out


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

# Subset of columns we will actually fill from auto-data.net
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
        ("autodata_url", "TEXT"),
        ("source_specs", "TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE cars ADD COLUMN {col} {kind}")
    conn.commit()


_NORM_RE = re.compile(r"[^a-z0-9]+")


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
    """Index DB rows under multiple keys so auto-data lookups land cleanly.

    Auto-data exposes (brand, model) like ("Acura", "ADX"); Wikidata stores
    the label as "Acura ADX" with manufacturer "Honda". So we register each
    row under several normalised keys covering common variations.
    """
    rows = conn.execute("SELECT qid, label, manufacturer FROM cars").fetchall()
    idx: dict[str, ExistingRow] = {}
    for qid, label, manuf in rows:
        if not label:
            continue
        candidates: set[str] = {label}
        # Strip a leading brand from the label, if any
        if manuf:
            low = label.lower()
            mlow = manuf.lower()
            if low.startswith(mlow + " "):
                candidates.add(label[len(manuf):].strip())
        # Strip parenthetical suffixes like "Acura Integra (DE)"
        no_paren = re.sub(r"\s*\([^)]*\)", "", label).strip()
        if no_paren:
            candidates.add(no_paren)

        row = ExistingRow(qid, label, manuf)
        for c in candidates:
            cn = _normalize(c)
            if cn:
                idx.setdefault(cn, row)
            if manuf:
                cb = _normalize(manuf + c)
                if cb:
                    idx.setdefault(cb, row)
    return idx


def _match(idx: dict[str, ExistingRow], brand: str, model: str) -> ExistingRow | None:
    # Try most-specific to least-specific
    for key in (
        _normalize(brand + " " + model),
        _normalize(brand + model),
        _normalize(model),
    ):
        if key and key in idx:
            return idx[key]
    return None


def upsert(conn: sqlite3.Connection, qid: str, brand: str, model: str, specs: dict, url: str) -> bool:
    """Upsert specs onto a row. Returns True if anything changed."""
    existing = conn.execute(
        f"SELECT label, manufacturer, {', '.join(_FILLABLE)} FROM cars WHERE qid=?", (qid,)
    ).fetchone()

    if existing is None:
        # Insert new row (auto-data only model)
        cols = ["qid", "label", "manufacturer", "autodata_url", "source_specs", "updated_at"] + list(_FILLABLE)
        vals: list = [qid, model, brand, url, "auto-data.net", time.strftime("%Y-%m-%d %H:%M:%S")]
        for f in _FILLABLE:
            vals.append(specs.get(f))
        placeholders = ",".join(["?"] * len(cols))
        conn.execute(
            f"INSERT INTO cars ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        return True

    label, _manuf, *cur_vals = existing
    cur = dict(zip(_FILLABLE, cur_vals))
    sets: list[str] = ["autodata_url=?"]
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
        params.append(("," if cur.get("source_specs") else "") + "auto-data:" + ",".join(src_marks))
        sets.append("updated_at=datetime('now')")
        params.append(qid)
        conn.execute(f"UPDATE cars SET {', '.join(sets)} WHERE qid=?", params)
        return True

    # Even with no fillable field updates, record the URL for traceability
    conn.execute("UPDATE cars SET autodata_url=? WHERE qid=?", (url, qid))
    return False


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


@dataclass
class ModelTask:
    brand_slug: str
    model_url: str


@dataclass
class ModelResult:
    brand_slug: str
    model_url: str
    brand: str
    model: str
    trim_url: str | None
    specs: dict


def _process_model(session: requests.Session, task: ModelTask) -> ModelResult | None:
    """Fetch generations + first trim for one model. Returns specs or None."""
    gens = list_generations(session, task.model_url)
    if not gens:
        return None
    picked_specs: dict = {}
    picked_url: str | None = None
    # Try up to 3 newest generations (last in chronological list)
    for gen_url in reversed(gens[-3:]):
        trims = list_trims(session, gen_url)
        if not trims:
            continue
        trim_html = fetch(session, BASE + trims[0])
        if not trim_html:
            continue
        specs = parse_trim_specs(trim_html)
        if specs:
            picked_specs = specs
            picked_url = trims[0]
            break
    if not picked_specs or not picked_url:
        return None
    brand = picked_specs.pop("_brand", task.brand_slug.title())
    model = picked_specs.pop("_model", task.model_url.split("/")[-1])
    for k in ("_generation", "_modification", "_powertrain"):
        picked_specs.pop(k, None)
    return ModelResult(task.brand_slug, task.model_url, brand, model, picked_url, picked_specs)


def crawl(
    db_path: Path,
    brand_limit: int = 0,
    model_limit: int = 0,
    delay: float = 0.0,  # delay between sequential ops (kept for compat); concurrency handles pacing
    resume: bool = True,
    workers: int = 8,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_columns(conn)
    idx = _build_match_index(conn)
    log.info("matched index: %d entries (cars in DB)", len(idx))

    bootstrap_session = requests.Session()
    brands = list_brands(bootstrap_session)
    log.info("brands found: %d", len(brands))
    if brand_limit:
        brands = brands[:brand_limit]

    # Each worker keeps its own Session so connection pools are per-thread.
    tls = threading.local()

    def _session() -> requests.Session:
        s = getattr(tls, "session", None)
        if s is None:
            s = requests.Session()
            tls.session = s
        return s

    # Step 1: enumerate (brand_slug, model_url) tasks in parallel.
    log.info("listing models for %d brands (workers=%d)...", len(brands), workers)
    tasks: list[ModelTask] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def _list_for_brand(burl: str) -> list[ModelTask]:
            slug = burl.split("/en/", 1)[-1].split("-brand-")[0]
            models = list_models(_session(), burl)
            if model_limit:
                models = models[:model_limit]
            return [ModelTask(slug, m) for m in models]

        futures = [pool.submit(_list_for_brand, b) for b in brands]
        for fut in tqdm(futures, desc="brands"):
            tasks.extend(fut.result())
    log.info("models discovered: %d", len(tasks))

    # Optional resume: skip models we've already populated (autodata_url already present)
    if resume:
        seen_urls = {
            row[0]
            for row in conn.execute(
                "SELECT autodata_url FROM cars WHERE autodata_url IS NOT NULL"
            )
        }
    else:
        seen_urls = set()

    inserted = 0
    updated = 0
    visited_trims = 0
    db_lock = threading.Lock()

    log.info("crawling trims (workers=%d)...", workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process_model, _session(), t) for t in tasks]
        for fut in tqdm(futures, desc="trims", total=len(futures)):
            try:
                result = fut.result()
            except Exception as e:
                log.debug("model task failed: %r", e)
                continue
            if not result:
                continue
            visited_trims += 1
            full_url = BASE + result.trim_url
            if full_url in seen_urls:
                continue

            existing = _match(idx, result.brand, result.model)
            qid = existing.qid if existing else f"ad:{result.model_url.rsplit('-model-', 1)[-1]}"

            with db_lock:
                try:
                    changed = upsert(conn, qid, result.brand, result.model, result.specs, full_url)
                    if changed:
                        if existing:
                            updated += 1
                        else:
                            inserted += 1
                            idx[_normalize(result.brand + result.model)] = ExistingRow(
                                qid, result.model, result.brand
                            )
                except sqlite3.Error as e:
                    log.warning("DB error for %s %s: %r", result.brand, result.model, e)
                if visited_trims % 200 == 0:
                    conn.commit()

    with db_lock:
        conn.commit()
    conn.close()
    log.info(
        "done: visited %d trim pages, inserted %d new rows, updated %d existing",
        visited_trims, inserted, updated,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).parent / "data" / "cars.db"))
    ap.add_argument("--brand-limit", type=int, default=0)
    ap.add_argument("--model-limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.0, help="(unused with concurrency)")
    ap.add_argument("--workers", type=int, default=8, help="parallel HTTP workers")
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()
    crawl(
        Path(args.db),
        args.brand_limit,
        args.model_limit,
        args.delay,
        not args.no_resume,
        workers=args.workers,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
