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
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
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
    ?wpUrlEn schema:about ?item ;
             schema:isPartOf <https://en.wikipedia.org/> .
  }
  OPTIONAL {
    ?wpUrlRu schema:about ?item ;
             schema:isPartOf <https://ru.wikipedia.org/> .
  }
  OPTIONAL {
    ?wpUrlDe schema:about ?item ;
             schema:isPartOf <https://de.wikipedia.org/> .
  }
  BIND(COALESCE(?wpUrlEn, ?wpUrlRu, ?wpUrlDe) AS ?wpUrl)
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


# =====================================================================
# Wikipedia-infobox enrichment.
#
# Wikidata exposes ~30% of length/width/height for car models. The same
# values are reliably present in Wikipedia infoboxes for per-generation
# articles (e.g. "BMW M3 (F80)"). This pass walks rows that have a
# wikipedia_url and any missing dimension/spec, downloads the page, parses
# the infobox table, normalises units (mm/kg/W) and back-fills empty
# columns. Existing Wikidata values win — enrichment never overwrites.
# =====================================================================

INFOBOX_HEADER_MAP = {
    # English
    "length": "length_mm",
    "width": "width_mm",
    "height": "height_mm",
    "wheelbase": "wheelbase_mm",
    "curb weight": "mass_kg",
    "kerb weight": "mass_kg",
    "kerb mass": "mass_kg",
    "weight": "mass_kg",
    "power output": "power_w",
    "power": "power_w",
    "engine": "engine",
    "engine type": "engine",
    "transmission": "transmission",
    "fuel type": "fuel_type",
    "fuel": "fuel_type",
    "powertrain": "drive_type",
    "layout": "drive_type",
    "body style": "body_style",
    "class": "body_style",
    "manufacturer": "manufacturer_wp",
    "production": "production_period",
    "doors": "doors",
    "displacement": "engine_displacement_cc",
    # Russian
    "длина": "length_mm",
    "ширина": "width_mm",
    "высота": "height_mm",
    "колёсная база": "wheelbase_mm",
    "колесная база": "wheelbase_mm",
    "масса": "mass_kg",
    "снаряжённая масса": "mass_kg",
    "снаряженная масса": "mass_kg",
    "полная масса": "mass_kg",
    "мощность": "power_w",
    "двигатель": "engine",
    "трансмиссия": "transmission",
    "коробка передач": "transmission",
    "привод": "drive_type",
    "компоновка": "drive_type",
    "класс": "body_style",
    "тип кузова": "body_style",
    "количество дверей": "doors",
    "число дверей": "doors",
    "объём двигателя": "engine_displacement_cc",
    "рабочий объём": "engine_displacement_cc",
    "топливо": "fuel_type",
    # German
    "länge": "length_mm",
    "breite": "width_mm",
    "höhe": "height_mm",
    "radstand": "wheelbase_mm",
    "leergewicht": "mass_kg",
    "leistung": "power_w",
    "motor": "engine",
    "getriebe": "transmission",
    "antrieb": "drive_type",
    "klasse": "body_style",
    "karosserieversion": "body_style",
    "türen": "doors",
    "hubraum": "engine_displacement_cc",
}

_LEN_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(mm|cm|m|in|ft|мм|см|м)\b", re.IGNORECASE)
_KG_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(kg|lb|tonnes?|t|кг|т)\b", re.IGNORECASE
)
_KW_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(kW|hp|PS|bhp|W|кВт|л\.?\s*с\.?)\b", re.IGNORECASE
)
_CC_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(cc|cm³|см³|cu\s*in|см3|cm3|L|l|л)\b", re.IGNORECASE
)


def _to_mm(value: float, unit: str) -> float | None:
    u = unit.lower().strip()
    if u in ("mm", "мм"):
        return value
    if u in ("cm", "см"):
        return value * 10
    if u in ("m", "м"):
        return value * 1000
    if u == "in":
        return value * 25.4
    if u == "ft":
        return value * 304.8
    return None


def _to_kg(value: float, unit: str) -> float | None:
    u = unit.lower().strip()
    if u in ("kg", "кг"):
        return value
    if u == "lb":
        return value * 0.453592
    if u in ("t", "т", "tonne", "tonnes"):
        return value * 1000
    return None


def _to_w(value: float, unit: str) -> float | None:
    u = unit.lower().strip().replace(" ", "").replace(".", "")
    if u == "w":
        return value
    if u in ("kw", "квт"):
        return value * 1000
    if u in ("hp", "bhp", "лс"):
        return value * 745.7
    if u == "ps":
        return value * 735.5
    return None


def _to_cc(value: float, unit: str) -> float | None:
    u = unit.lower().strip()
    if u in ("cc", "cm³", "см³", "cm3", "см3"):
        return value
    if u in ("l", "л"):
        return value * 1000
    if u == "cu in":
        return value * 16.387
    return None


def _first_match(rx: re.Pattern, text: str, conv) -> float | None:
    text_clean = text.replace("\xa0", " ").replace(",", "")
    m = rx.search(text_clean)
    if not m:
        return None
    try:
        return conv(float(m.group(1)), m.group(2))
    except (TypeError, ValueError):
        return None


def parse_infobox(html: str) -> dict:
    """Return a dict of normalized fields parsed from the page infobox."""
    soup = BeautifulSoup(html, "lxml")
    box = soup.select_one("table.infobox")
    if not box:
        return {}

    raw: dict[str, str] = {}
    for row in box.find_all("tr"):
        th = row.find("th")
        td = row.find("td")
        if not th or not td:
            continue
        header = th.get_text(" ", strip=True).lower().strip().rstrip(":").rstrip("·")
        # Some infoboxes group dimensions inside a nested table.
        nested = td.find("table")
        if nested:
            for sub in nested.find_all("tr"):
                sth = sub.find("th")
                std = sub.find("td")
                if sth and std:
                    sub_h = sth.get_text(" ", strip=True).lower().strip().rstrip(":")
                    if sub_h in INFOBOX_HEADER_MAP and INFOBOX_HEADER_MAP[sub_h] not in raw:
                        raw[INFOBOX_HEADER_MAP[sub_h]] = std.get_text(" ", strip=True)
        if header in INFOBOX_HEADER_MAP and INFOBOX_HEADER_MAP[header] not in raw:
            raw[INFOBOX_HEADER_MAP[header]] = td.get_text(" ", strip=True)

    out: dict = {}
    for k, text in raw.items():
        if k.endswith("_mm"):
            v = _first_match(_LEN_RE, text, _to_mm)
            if v and 100 < v < 100000:
                out[k] = round(v, 1)
        elif k == "mass_kg":
            v = _first_match(_KG_RE, text, _to_kg)
            if v and 100 < v < 50000:
                out[k] = round(v, 1)
        elif k == "power_w":
            v = _first_match(_KW_RE, text, _to_w)
            if v and 1000 < v < 5_000_000:
                out[k] = round(v, 1)
        elif k == "engine_displacement_cc":
            v = _first_match(_CC_RE, text, _to_cc)
            if v and 50 < v < 20000:
                out[k] = round(v, 1)
        elif k == "doors":
            m = re.search(r"\d+", text)
            if m:
                try:
                    out[k] = int(m.group(0))
                except ValueError:
                    pass
        else:
            cleaned = text.split("[")[0].strip()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if cleaned:
                out[k] = cleaned[:300]
    return out


def fetch_wikipedia(url: str, session: requests.Session) -> str | None:
    try:
        r = session.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        if r.status_code == 200:
            return r.text
    except requests.RequestException as e:
        log.debug("wp fetch failed for %s: %r", url, e)
    return None


_ENRICH_FIELDS = (
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
    "transmission",
    "production_period",
)


def _ensure_enrich_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cars)").fetchall()}
    if "transmission" not in cols:
        conn.execute("ALTER TABLE cars ADD COLUMN transmission TEXT")
    if "production_period" not in cols:
        conn.execute("ALTER TABLE cars ADD COLUMN production_period TEXT")
    conn.commit()


def enrich_from_wikipedia(
    conn: sqlite3.Connection,
    *,
    limit: int = 0,
    only_missing: bool = True,
    delay: float = 0.05,
) -> tuple[int, int]:
    """Fetch each row's Wikipedia page and back-fill empty fields from infobox.

    Returns (rows_processed, rows_updated).
    """
    _ensure_enrich_columns(conn)

    if only_missing:
        # Pick rows that have a Wikipedia URL and at least one core dim missing.
        sql = """
            SELECT qid, wikipedia_url
            FROM cars
            WHERE wikipedia_url IS NOT NULL AND wikipedia_url != ''
              AND (length_mm IS NULL OR width_mm IS NULL
                   OR height_mm IS NULL OR wheelbase_mm IS NULL
                   OR mass_kg IS NULL OR body_style IS NULL
                   OR drive_type IS NULL OR engine IS NULL)
            ORDER BY qid
        """
    else:
        sql = """
            SELECT qid, wikipedia_url
            FROM cars
            WHERE wikipedia_url IS NOT NULL AND wikipedia_url != ''
            ORDER BY qid
        """
    rows = conn.execute(sql).fetchall()
    if limit:
        rows = rows[:limit]

    log.info("enrichment: %d rows to process", len(rows))
    session = requests.Session()
    updated = 0
    for qid, url in tqdm(rows, desc="WP enrichment"):
        html = fetch_wikipedia(url, session)
        if not html:
            time.sleep(delay)
            continue
        info = parse_infobox(html)
        if not info:
            time.sleep(delay)
            continue

        # Build conditional UPDATE that only fills NULL/empty cells.
        existing = conn.execute(
            f"SELECT {', '.join(_ENRICH_FIELDS)} FROM cars WHERE qid=?",
            (qid,),
        ).fetchone()
        if not existing:
            time.sleep(delay)
            continue
        existing_map = dict(zip(_ENRICH_FIELDS, existing))

        sets = []
        params = []
        for field, new_val in info.items():
            if field not in _ENRICH_FIELDS:
                continue
            cur = existing_map.get(field)
            if cur is None or cur == "" or cur == 0:
                sets.append(f"{field}=?")
                params.append(new_val)
        if sets:
            sets.append("updated_at=datetime('now')")
            params.append(qid)
            conn.execute(
                f"UPDATE cars SET {', '.join(sets)} WHERE qid=?",
                params,
            )
            updated += 1
            if updated % 200 == 0:
                conn.commit()
        time.sleep(delay)

    conn.commit()
    log.info("enrichment done: %d rows updated of %d", updated, len(rows))
    return len(rows), updated


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to SQLite DB")
    ap.add_argument("--limit", type=int, default=0, help="limit to first N IDs (debug)")
    ap.add_argument(
        "--enrich-only",
        action="store_true",
        help="skip Wikidata SPARQL pass and only run Wikipedia infobox enrichment",
    )
    ap.add_argument(
        "--no-enrich",
        action="store_true",
        help="skip Wikipedia infobox enrichment after the SPARQL pass",
    )
    ap.add_argument(
        "--enrich-all",
        action="store_true",
        help="re-enrich every row, not only those with missing dims",
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    conn = init_db(db_path)
    _ensure_enrich_columns(conn)

    if not args.enrich_only:
        log.info("1/4 collecting model IDs from Wikidata...")
        ids = collect_model_ids()
        if args.limit:
            ids = ids[: args.limit]
        log.info("   total models: %d", len(ids))
        if not ids:
            log.error("nothing fetched, aborting")
            return 1

        log.info("2/4 fetching details in batches of %d...", DETAIL_BATCH)
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

        log.info("3/4 rebuilding FTS index...")
        rebuild_fts(conn)
        set_meta(conn, "total_models", str(total_written))

    if not args.no_enrich:
        log.info("4/4 enriching from Wikipedia infoboxes...")
        enrich_from_wikipedia(conn, limit=args.limit, only_missing=not args.enrich_all)
        rebuild_fts(conn)

    set_meta(
        conn,
        "last_updated_utc",
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    (total,) = conn.execute("SELECT COUNT(*) FROM cars").fetchone()
    log.info("Total in DB: %d", total)
    stats_fields = list(FIELDS) + ["transmission", "production_period"]
    for col in stats_fields:
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
