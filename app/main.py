"""Composition root: the FastAPI app wiring core + modules, plus HTTP endpoints.

The entry point (`uvicorn app.main:app`). `python -c "import app.main"` must succeed, so
nothing here may contact Home Assistant or do work at import time.

Core never imports a module: modules are attached here, through the four seams described
in `app.modules`, so a module can be replaced or added without touching the trunk.

P0 wires nothing yet. The app serves GET /healthz and the static UI shell; core services
and the Adaptive Light module are attached in later phases.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# FastAPI's auto-generated /docs, /redoc and /openapi.json are disabled so the HTTP surface
# is exactly what this file declares, with nothing implicit.
app = FastAPI(title="Light Map", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness only: says the process is serving, nothing about Home Assistant."""
    return {"status": "ok"}


# Mounted last: a mount at "/" matches every path, so any route declared after it would be
# shadowed by the static files.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
