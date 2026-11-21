"""Aplicación FastAPI del laboratorio (Fase 01).

Punto de entrada: ``uvicorn api.main:app --reload`` (con deps instaladas).
En modo mock (por defecto) no requiere credenciales ni servicios externos.

Criterio de aceptación: la API inicia localmente en modo mock.
"""

from __future__ import annotations

from api import _require_fastapi

_require_fastapi()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from api.routes.health import router as health_router
from api.routes.triage import router as triage_router
from config import get_config


def create_app() -> FastAPI:
    cfg = get_config()
    app = FastAPI(
        title="AI Help Desk — Lab API",
        description="Backend HTTP del laboratorio agentic. Modo mock por defecto.",
        version="0.1.0",
    )
    app.include_router(health_router)
    app.include_router(triage_router)

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.exception_handler(Exception)
    async def _generic_exc(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": {"type": type(exc).__name__, "message": str(exc)}})

    @app.on_event("startup")
    def _startup() -> None:
        # Log mínimo de modo al arrancar.
        mode = "mock" if cfg.mock_mode else "live"
        print(f"[lab-api] starting in {mode} mode (llm={cfg.llm_provider})")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    cfg = get_config()
    uvicorn.run("api.main:app", host=cfg.api_host, port=cfg.api_port, reload=True)
