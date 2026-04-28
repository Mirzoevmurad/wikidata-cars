"""FastAPI web app — поиск и сравнение моделей авто из локальной SQLite БД."""
from __future__ import annotations

import csv
import io
import os
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
    return {k: row[k] for k in row.keys()}


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
            fts_query = _escape_fts(q)
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
            # fallback LIKE if FTS returned nothing
            if not rows:
                like = f"%{q}%"
                rows = conn.execute(
                    """
                    SELECT qid, label, manufacturer, inception FROM cars
                    WHERE label LIKE ? OR manufacturer LIKE ?
                    ORDER BY label LIMIT ?
                    """,
                    (like, like, limit),
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


@app.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request, qids: str = "") -> HTMLResponse:
    ids = [x.strip() for x in qids.split(",") if x.strip()]
    models: list[dict[str, Any]] = []
    if ids:
        conn = get_conn()
        try:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM cars WHERE qid IN ({placeholders})", ids
            ).fetchall()
            by_qid = {r["qid"]: _row_to_dict(r) for r in rows}
            models = [by_qid.get(q, {"qid": q, "label": "(not found)"}) for q in ids]
        finally:
            conn.close()
    return templates.TemplateResponse(
        request,
        "compare.html",
        {"models": models, "qids_raw": qids},
    )


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    ok = DB_PATH.exists()
    return {"ok": ok, "db": str(DB_PATH)}
