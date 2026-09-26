"""Standalone Strategy Lab API — the /lab routes with NO feed, NO loops.

For verifying or using the Lab (catalog, readiness, backtests, ledgers)
without touching a live Tradewell process: the main backend keeps the one
KiteTicker and all the state loops; this app only reads/writes the lab's
own stores and the read-only historical DBs. Safe to run alongside the
live stack on another port:

    .venv/bin/uvicorn app.lab.preview_api:app --port 8001
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_lab
from app.lab.service import ensure_seeded

app = FastAPI(title="Tradewell Strategy Lab (standalone)", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000",
                   "http://localhost:3001", "http://127.0.0.1:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
ensure_seeded()
app.include_router(routes_lab.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": "lab-standalone"}
