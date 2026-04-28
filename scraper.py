#!/usr/bin/env python3
"""
scraper.py — собирает все модели автомобилей из Wikidata (Q3231690 + подклассы)
и сохраняет в SQLite (таблица + FTS5-индекс по названию/производителю).

Запуск:
    python scraper.py                    # обновить/создать data/cars.db
    python scraper.py --db /path/cars.db # указать путь к БД
    python scraper.py --limit 500        # обработать только первые N ID (для тестов)
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

import requests
from tqdm import tqdm

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = (
    "WikidataCarsScraper/1.0 "
    "(https://github.com/Mirzoevmurad/wikidata-cars; mirzoevmurad@gmail.com)"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/sparql-results+json",
}

DEFAULT_DB = Path(__file__).parent / "data" / "cars.db"

ID_CHUNK = 10000
DETAIL_BATCH = 80
POLITE_DELAY = 0.4
RETRIES = 6
BACKOFF = 10

# NB: units in Wikidata for cars are typically:
#   length / width / height / wheelbase: millimetres
#   mass: kilograms
#   power: watts (convert /1000 for kW if нужно)
#   engine_displacement: cubic centimetres
# We store raw numeric values as-is from Wikidata (no per-row unit conversion).
FIELDS = [
    "qid",
    "label",
    "manufacturer",
    "inception",
    "production_start",
    "discontinued",
    "body_style",
    "mass_kg",
    "power_w",
    "length_mm",
    "width_mm",
    "height_mm",
    "wheelbase_mm",
    "engine_displacement_cc",
    "engine",
    "fuel_type",
    "drive_type",
    "doors",
    "max_speed",
    "total_produced",
    "image_url",
    "wikipedia_url",
]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("scraper")


def run_query(query: str):
    last_err = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(
                ENDPOINT,
                params={"query": query, "format": "json"},
                headers=HEADERS,
                timeout=180,
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", BACKOFF * (attempt + 1)))
                log.warning("429 from Wikidata, sleeping %ss", wait)
                time.sleep(wait)
                continue
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            last_err = repr(e)
        time.sleep(BACKOFF * (attempt + 1))
    log.warning("SPARQL query failed after %d attempts: %s", RETRIES, last_err)
    return None


def collect_model_ids() -> list[str]:
    ids: list[str] = []
    offset = 0
    pbar = tqdm(desc="IDs", unit="id")
    while True:
        q = f"""
        SELECT ?item WHERE {{
          ?item wdt:P31/wdt:P279* wd:Q3231690 .
        }}
        ORDER BY ?item
        LIMIT {ID_CHUNK} OFFSET {offset}
        """
        data = run_query(q)
        if not data:
            break
        rows = data["results"]["bindings"]
        if not rows:
            break
        for row in rows:
            ids.append(row["item"]["value"].rsplit("/", 1)[-1])
        pbar.update(len(rows))
        if len(rows) < ID_CHUNK:
            break
        offset += ID_CHUNK
        time.sleep(POLITE_DELAY)
    pbar.close()
    return sorted(set(ids))


# Wikidata property map (many car-spec properties don't exist at the model level;
# "powered by" P516 is the closest thing to fuel/engine type). Zero-coverage
# props (P5101 body style, P5572 fuel, P8034 drive, P1301 doors) are kept as
# OPTIONAL so if/when they get populated we pick them up automatically.
DETAIL_QUERY_TEMPLATE = """
SELECT ?item ?itemLabel
       ?manufacturerLabel
       ?inception ?productionStart ?discontinued
       ?bodyStyleLabel
       ?mass ?power ?length ?width ?height ?wheelbase ?displacement
       ?engineLabel ?fuelLabel ?driveLabel ?doors
       ?maxSpeed ?totalProduced
       ?imageUrl ?wpUrl
WHERE {
  VALUES ?item { %s }
  OPTIONAL { ?item wdt:P176  ?manufacturer. }
  OPTIONAL { ?item wdt:P571  ?inception. }
  OPTIONAL { ?item wdt:P580  ?productionStart. }
  OPTIONAL { ?item wdt:P2669 ?discontinued. }
  OPTIONAL { ?item wdt:P5101 ?bodyStyle. }
  OPTIONAL { ?item wdt:P2067 ?mass. }
  OPTIONAL { ?item wdt:P2791 ?power. }
  OPTIONAL { ?item wdt:P2043 ?length. }
  OPTIONAL { ?item wdt:P2049 ?width. }
  OPTIONAL { ?item wdt:P2048 ?height. }
  OPTIONAL { ?item wdt:P3039 ?wheelbase. }
  OPTIONAL { ?item wdt:P2808 ?displacement. }
  OPTIONAL { ?item wdt:P516  ?engine. }
  OPTIONAL { ?item wdt:P5572 ?fuel. }
  OPTIONAL { ?item wdt:P8034 ?drive. }
  OPTIONAL { ?item wdt:P1301 ?doors. }
  OPTIONAL { ?item wdt:P2052 ?maxSpeed. }
  OPTIONAL { ?item wdt:P1092 ?totalProduced. }
  OPTIONAL { ?item wdt:P18   ?imageUrl. }
  OPTIONAL {
    ?wpUrl schema:about ?item ;
           schema:isPartOf <https://en.wikipedia.org/> .
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "ru,en,de,fr,es". }
}
"""


def _val(binding: dict, key: str):
    v = binding.get(key)
    return v["value"] if v else None


def parse_batch(bindings: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    mapping = [
        ("label", "itemLabel"),
        ("manufacturer", "manufacturerLabel"),
        ("inception", "inception"),
        ("production_start", "productionStart"),
        ("discontinued", "discontinued"),
        ("body_style", "bodyStyleLabel"),
        ("mass_kg", "mass"),
        ("power_w", "power"),
        ("length_mm", "length"),
        ("width_mm", "width"),
        ("height_mm", "height"),
        ("wheelbase_mm", "wheelbase"),
        ("engine_displacement_cc", "displacement"),
        ("engine", "engineLabel"),
        ("fuel_type", "fuelLabel"),
        ("drive_type", "driveLabel"),
        ("doors", "doors"),
        ("max_speed", "maxSpeed"),
        ("total_produced", "totalProduced"),
        ("image_url", "imageUrl"),
        ("wikipedia_url", "wpUrl"),
    ]
    for b in bindings:
        item_uri = _val(b, "item")
        if not item_uri:
            continue
        qid = item_uri.rsplit("/", 1)[-1]
        rec = out.setdefault(qid, {f: None for f in FIELDS})
        rec["qid"] = qid
        for dst, src in mapping:
            if rec[dst] is None:
                rec[dst] = _val(b, src)
    return out


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cars (
    qid TEXT PRIMARY KEY,
    label TEXT,
    manufacturer TEXT,
    inception TEXT,
    production_start TEXT,
    discontinued TEXT,
    body_style TEXT,
    mass_kg REAL,
    power_w REAL,
    length_mm REAL,
    width_mm REAL,
    height_mm REAL,
    wheelbase_mm REAL,
    engine_displacement_cc REAL,
    engine TEXT,
    fuel_type TEXT,
    drive_type TEXT,
    doors INTEGER,
    max_speed REAL,
    total_produced REAL,
    image_url TEXT,
    wikipedia_url TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS cars_fts USING fts5(
    qid UNINDEXED,
    label,
    manufacturer,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _to_float(x):
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_int(x):
    if x is None:
        return None
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def upsert_records(conn: sqlite3.Connection, records: list[dict]) -> None:
    rows = [
        (
            r["qid"],
            r.get("label"),
            r.get("manufacturer"),
            r.get("inception"),
            r.get("production_start"),
            r.get("discontinued"),
            r.get("body_style"),
            _to_float(r.get("mass_kg")),
            _to_float(r.get("power_w")),
            _to_float(r.get("length_mm")),
            _to_float(r.get("width_mm")),
            _to_float(r.get("height_mm")),
            _to_float(r.get("wheelbase_mm")),
            _to_float(r.get("engine_displacement_cc")),
            r.get("engine"),
            r.get("fuel_type"),
            r.get("drive_type"),
            _to_int(r.get("doors")),
            _to_float(r.get("max_speed")),
            _to_float(r.get("total_produced")),
            r.get("image_url"),
            r.get("wikipedia_url"),
        )
        for r in records
    ]
    conn.executemany(
        """
        INSERT INTO cars (
            qid, label, manufacturer,
            inception, production_start, discontinued,
            body_style, mass_kg, power_w,
            length_mm, width_mm, height_mm, wheelbase_mm,
            engine_displacement_cc, engine, fuel_type, drive_type, doors,
            max_speed, total_produced, image_url, wikipedia_url,
            updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(qid) DO UPDATE SET
            label=excluded.label,
            manufacturer=excluded.manufacturer,
            inception=excluded.inception,
            production_start=excluded.production_start,
            discontinued=excluded.discontinued,
            body_style=excluded.body_style,
            mass_kg=excluded.mass_kg,
            power_w=excluded.power_w,
            length_mm=excluded.length_mm,
            width_mm=excluded.width_mm,
            height_mm=excluded.height_mm,
            wheelbase_mm=excluded.wheelbase_mm,
            engine_displacement_cc=excluded.engine_displacement_cc,
            engine=excluded.engine,
            fuel_type=excluded.fuel_type,
            drive_type=excluded.drive_type,
            doors=excluded.doors,
            max_speed=excluded.max_speed,
            total_produced=excluded.total_produced,
            image_url=excluded.image_url,
            wikipedia_url=excluded.wikipedia_url,
            updated_at=datetime('now')
        """,
        rows,
    )
    conn.commit()


def rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM cars_fts;")
    conn.execute(
        """
        INSERT INTO cars_fts (qid, label, manufacturer)
        SELECT qid, COALESCE(label, ''), COALESCE(manufacturer, '')
        FROM cars;
        """
    )
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to SQLite DB")
    ap.add_argument("--limit", type=int, default=0, help="limit to first N IDs (debug)")
    args = ap.parse_args()

    db_path = Path(args.db)
    conn = init_db(db_path)

    log.info("1/3 collecting model IDs from Wikidata...")
    ids = collect_model_ids()
    if args.limit:
        ids = ids[: args.limit]
    log.info("   total models: %d", len(ids))
    if not ids:
        log.error("nothing fetched, aborting")
        return 1

    log.info("2/3 fetching details in batches of %d...", DETAIL_BATCH)
    total_written = 0
    for i in tqdm(range(0, len(ids), DETAIL_BATCH), desc="Details"):
        chunk = ids[i : i + DETAIL_BATCH]
        values = " ".join(f"wd:{q}" for q in chunk)
        data = run_query(DETAIL_QUERY_TEMPLATE % values)
        bindings = data["results"]["bindings"] if data else []
        parsed = parse_batch(bindings)
        records = []
        for qid in chunk:
            rec = parsed.get(qid) or {f: None for f in FIELDS}
            rec["qid"] = qid
            records.append(rec)
        upsert_records(conn, records)
        total_written += len(records)
        time.sleep(POLITE_DELAY)

    log.info("3/3 rebuilding FTS index and stats...")
    rebuild_fts(conn)
    set_meta(conn, "last_updated_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    set_meta(conn, "total_models", str(total_written))

    (total,) = conn.execute("SELECT COUNT(*) FROM cars").fetchone()
    log.info("Total in DB: %d", total)
    for col in FIELDS:
        if col == "qid":
            continue
        (filled,) = conn.execute(
            f"SELECT COUNT(*) FROM cars WHERE {col} IS NOT NULL AND {col} != ''"
        ).fetchone()
        pct = filled / total * 100 if total else 0
        log.info("  %-25s %7d  (%.1f%%)", col, filled, pct)

    conn.close()
    log.info("DB saved: %s", db_path.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
