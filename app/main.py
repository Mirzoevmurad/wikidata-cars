"""FastAPI web app — поиск и сравнение моделей авто из локальной SQLite БД."""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
DB_PATH = Path(os.environ.get("CARS_DB", REPO_ROOT / "data" / "cars.db"))

app = FastAPI(title="Wikidata Cars")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Database {DB_PATH} not found. Run `python scraper.py` first."
            ),
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    out["display_name"] = _display_name(out.get("label"), out.get("manufacturer"))
    return out


_CORP_SUFFIX_RE = re.compile(
    # Require an actual separator (',' or whitespace) before the suffix so
    # 'Iveco' isn't trimmed to 'Ive' (the trailing 'co' would otherwise match).
    r"(?:[,]\s*|\s+)"
    r"(?:motor company|motor corporation|motor corp|motors|motor|"
    r"corporation|company|holdings|holding|group|automobile|automotive|"
    r"vehicles|cars|inc|incorporated|llc|ltd|limited|plc|corp|co|"
    r"ag|gmbh|kg|sa|s\.p\.a|s\.r\.l|n\.v|b\.v|kk|jsc|ojsc|pao|oao|ooo)"
    r"\.?\s*$",
    re.IGNORECASE,
)


def _brand_short(manufacturer: str) -> str:
    """Strip trailing legal/corporate suffixes from a manufacturer name.

    'Tesla, Inc.' -> 'Tesla', 'Toyota Motor Corporation' -> 'Toyota',
    'BMW AG' -> 'BMW', 'Audi' -> 'Audi'.
    """
    name = (manufacturer or "").strip()
    if not name:
        return ""
    while True:
        new = _CORP_SUFFIX_RE.sub("", name).strip(" ,.")
        if new == name or not new:
            break
        name = new
    return name


def _display_name(label: str | None, manufacturer: str | None) -> str:
    """Return '<Brand> <Model>' for the UI.

    Wikidata labels often already include the brand (e.g. 'Tesla Model X')
    while drom.ru / auto.ru / auto-data rows store only the model in `label`
    (e.g. 'A1', 'Camry'). We compare the label against a short form of the
    manufacturer (without ', Inc.', 'Motor Corporation', 'AG' …) and prepend
    that short form when the label doesn't already start with it.
    """
    label = (label or "").strip()
    manuf = (manufacturer or "").strip()
    if not label:
        return manuf
    if not manuf:
        return label
    brand = _brand_short(manuf) or manuf
    low_label = label.lower()
    if low_label.startswith(brand.lower()) or low_label.startswith(manuf.lower()):
        return label
    return f"{brand} {label}"


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for k, v in conn.execute("SELECT key, value FROM meta"):
            out[k] = v
    except sqlite3.OperationalError:
        pass
    try:
        (total,) = conn.execute("SELECT COUNT(*) FROM cars").fetchone()
        out["total_models"] = str(total)
    except sqlite3.OperationalError:
        out["total_models"] = "0"
    return out


def _escape_fts(query: str) -> str:
    # Escape FTS5 special chars by quoting each token.
    tokens = [t for t in query.replace('"', " ").split() if t]
    if not tokens:
        return ""
    return " ".join(f'"{t}"*' for t in tokens)


# ЙЦУКЕН → QWERTY: keys at the same physical position on a Russian keyboard.
# Used for typo-tolerant search: typing 'лшф кшщ' (the keys you'd hit if your
# layout were stuck on Russian while you tried to type 'kia rio') still finds
# 'Kia Rio'. Letters only — search shouldn't care about punctuation.
_RU_EN_PAIRS = (
    ("а", "f"), ("б", ","), ("в", "d"), ("г", "u"), ("д", "l"),
    ("е", "t"), ("ё", "`"), ("ж", ";"), ("з", "p"), ("и", "b"),
    ("й", "q"), ("к", "r"), ("л", "k"), ("м", "v"), ("н", "y"),
    ("о", "j"), ("п", "g"), ("р", "h"), ("с", "c"), ("т", "n"),
    ("у", "e"), ("ф", "a"), ("х", "["), ("ц", "w"), ("ч", "x"),
    ("ш", "i"), ("щ", "o"), ("ъ", "]"), ("ы", "s"), ("ь", "m"),
    ("э", "'"), ("ю", "."), ("я", "z"),
)
_RU_TO_EN = {ord(ru): en for ru, en in _RU_EN_PAIRS}
_RU_TO_EN.update({ord(ru.upper()): en.upper() for ru, en in _RU_EN_PAIRS if ru.isalpha()})
_EN_TO_RU = {ord(en): ru for ru, en in _RU_EN_PAIRS}
_EN_TO_RU.update({ord(en.upper()): ru.upper() for ru, en in _RU_EN_PAIRS if en.isalpha()})


def _swap_layout(text: str) -> str:
    """Return `text` with each char remapped between RU↔EN keyboard layouts.

    If the input contains both alphabets we still translate each character
    individually — the result is a best-effort that lets FTS pick up matches
    when the user typed in the wrong layout.
    """
    if not text:
        return ""
    has_cyr = any("\u0400" <= ch <= "\u04ff" for ch in text)
    return text.translate(_RU_TO_EN if has_cyr else _EN_TO_RU)


def _build_fts_query(query: str) -> str:
    """Build an FTS5 MATCH expression for `query`, OR-ing in a layout-swapped
    variant so users with the wrong keyboard layout still get hits."""
    primary = _escape_fts(query)
    swapped_text = _swap_layout(query)
    if swapped_text and swapped_text != query:
        secondary = _escape_fts(swapped_text)
        if secondary and secondary != primary:
            return f"({primary}) OR ({secondary})"
    return primary


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    conn = get_conn()
    meta = _meta(conn)
    conn.close()
    return templates.TemplateResponse(
        request,
        "index.html",
        {"meta": meta},
    )


@app.get("/api/search")
def api_search(
    q: str = Query("", description="free-text query (model or manufacturer)"),
    limit: int = Query(30, ge=1, le=200),
) -> dict[str, Any]:
    conn = get_conn()
    try:
        q = q.strip()
        if not q:
            rows = conn.execute(
                "SELECT qid, label, manufacturer, inception FROM cars "
                "ORDER BY label LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            fts_query = _build_fts_query(q)
            if fts_query:
                rows = conn.execute(
                    """
                    SELECT c.qid, c.label, c.manufacturer, c.inception
                    FROM cars_fts f
                    JOIN cars c ON c.qid = f.qid
                    WHERE cars_fts MATCH ?
                    ORDER BY bm25(cars_fts)
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
            else:
                rows = []
            # fallback LIKE if FTS returned nothing — also try a layout-swapped
            # version of the query so 'лшф кшщ' falls back to 'kia rio'.
            if not rows:
                candidates = {q}
                swapped = _swap_layout(q)
                if swapped and swapped != q:
                    candidates.add(swapped)
                clauses, params = [], []
                for c in candidates:
                    like = f"%{c}%"
                    clauses.append("(label LIKE ? OR manufacturer LIKE ?)")
                    params.extend([like, like])
                params.append(limit)
                rows = conn.execute(
                    f"""
                    SELECT qid, label, manufacturer, inception FROM cars
                    WHERE {' OR '.join(clauses)}
                    ORDER BY label LIMIT ?
                    """,
                    params,
                ).fetchall()
        return {"query": q, "count": len(rows), "results": [_row_to_dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/model/{qid}")
def api_model(qid: str) -> dict[str, Any]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM cars WHERE qid = ?", (qid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"model {qid} not found")
        return _row_to_dict(row)
    finally:
        conn.close()


def _fetch_generations(conn: sqlite3.Connection, qids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Return {qid: [generation, …]} for the given qids. Each generation has
    year_start / year_end / body_style / source / source_url. The
    ``generations`` table is created lazily by the enrichment scrapers, so we
    tolerate it not existing yet."""
    out: dict[str, list[dict[str, Any]]] = {q: [] for q in qids}
    if not qids:
        return out
    try:
        placeholders = ",".join("?" for _ in qids)
        rows = conn.execute(
            f"""SELECT qid, gen_index, year_start, year_end, body_style,
                       source, source_url
                FROM generations
                WHERE qid IN ({placeholders})
                ORDER BY qid, year_start, gen_index""",
            qids,
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    # Dedupe by (qid, year_start, year_end, body_style) — naavtotrasse and
    # chinamobil sometimes overlap.
    seen: dict[tuple[str, int | None, int | None, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["qid"], r["year_start"], r["year_end"], r["body_style"] or "")
        if key in seen:
            continue
        seen[key] = {
            "qid": r["qid"],
            "gen_index": r["gen_index"],
            "year_start": r["year_start"],
            "year_end": r["year_end"],
            "body_style": r["body_style"],
            "source": r["source"],
            "source_url": r["source_url"],
        }
    for d in seen.values():
        out.setdefault(d["qid"], []).append(d)
    for q in out:
        out[q].sort(
            key=lambda g: (
                g["year_start"] or 0,
                # "present" (year_end NULL) ranks higher than a fixed end year
                1 if g["year_end"] is None else 0,
                g["year_end"] or 0,
            )
        )
    return out


@app.get("/api/generations")
def api_generations(qids: str = Query(..., description="comma-separated QIDs")) -> dict[str, Any]:
    ids = [x.strip() for x in qids.split(",") if x.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="empty qids")
    conn = get_conn()
    try:
        return {"generations": _fetch_generations(conn, ids)}
    finally:
        conn.close()


@app.get("/api/compare")
def api_compare(qids: str = Query(..., description="comma-separated QIDs")) -> dict[str, Any]:
    ids = [x.strip() for x in qids.split(",") if x.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="empty qids")
    if len(ids) > 10:
        raise HTTPException(status_code=400, detail="max 10 models")
    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM cars WHERE qid IN ({placeholders})", ids
        ).fetchall()
        by_qid = {r["qid"]: _row_to_dict(r) for r in rows}
        return {"models": [by_qid.get(q, {"qid": q, "not_found": True}) for q in ids]}
    finally:
        conn.close()


@app.get("/api/stats")
def api_stats() -> dict[str, Any]:
    conn = get_conn()
    try:
        meta = _meta(conn)
        fields = [
            "label", "manufacturer", "inception", "production_start", "discontinued",
            "body_style", "mass_kg", "power_w",
            "length_mm", "width_mm", "height_mm", "wheelbase_mm",
            "engine_displacement_cc", "engine", "fuel_type", "drive_type", "doors",
            "max_speed", "total_produced", "image_url", "wikipedia_url",
        ]
        (total,) = conn.execute("SELECT COUNT(*) FROM cars").fetchone()
        coverage = {}
        for f in fields:
            (n,) = conn.execute(
                f"SELECT COUNT(*) FROM cars WHERE {f} IS NOT NULL AND {f} != ''"
            ).fetchone()
            coverage[f] = {"filled": n, "pct": round(n / total * 100, 1) if total else 0}
        return {"total": total, "meta": meta, "coverage": coverage}
    finally:
        conn.close()


@app.get("/export.csv")
def export_csv() -> StreamingResponse:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT qid, label, manufacturer, inception, production_start, "
            "discontinued, body_style, mass_kg, power_w, "
            "length_mm, width_mm, height_mm, wheelbase_mm, "
            "engine_displacement_cc, engine, fuel_type, drive_type, doors, "
            "max_speed, total_produced, image_url, wikipedia_url "
            "FROM cars ORDER BY label"
        ).fetchall()
        cols = list(rows[0].keys()) if rows else []
    finally:
        conn.close()

    buf = io.StringIO()
    buf.write("\ufeff")  # BOM for Excel
    w = csv.writer(buf)
    if cols:
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="wikidata_cars.csv"'},
    )


def _timeline_bounds(gens_by_qid: dict[str, list[dict[str, Any]]]) -> tuple[int, int]:
    """Pick the (min year_start, max year_end-or-current) across all generations
    so the per-model timeline bars share the same time axis."""
    starts = []
    ends = []
    import datetime as _dt
    current = _dt.datetime.utcnow().year
    for gens in gens_by_qid.values():
        for g in gens:
            if g.get("year_start"):
                starts.append(int(g["year_start"]))
            if g.get("year_end"):
                ends.append(int(g["year_end"]))
            else:
                ends.append(current)
    if not starts or not ends:
        return current, current
    lo = min(starts)
    hi = max(ends)
    if hi <= lo:
        hi = lo + 1
    return lo, hi


@app.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request, qids: str = "") -> HTMLResponse:
    ids = [x.strip() for x in qids.split(",") if x.strip()]
    models: list[dict[str, Any]] = []
    generations: dict[str, list[dict[str, Any]]] = {}
    timeline_lo = timeline_hi = 0
    has_any_generations = False
    if ids:
        conn = get_conn()
        try:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM cars WHERE qid IN ({placeholders})", ids
            ).fetchall()
            by_qid = {r["qid"]: _row_to_dict(r) for r in rows}
            models = [
                by_qid.get(q, {"qid": q, "label": "(not found)", "display_name": "(not found)"})
                for q in ids
            ]
            generations = _fetch_generations(conn, ids)
            has_any_generations = any(generations.get(q) for q in ids)
            if has_any_generations:
                timeline_lo, timeline_hi = _timeline_bounds(generations)
        finally:
            conn.close()
    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "models": models,
            "qids_raw": qids,
            "generations": generations,
            "has_any_generations": has_any_generations,
            "timeline_lo": timeline_lo,
            "timeline_hi": timeline_hi,
        },
    )


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    ok = DB_PATH.exists()
    return {"ok": ok, "db": str(DB_PATH)}
