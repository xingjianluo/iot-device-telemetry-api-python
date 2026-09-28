"""FastAPI application factory: routers and uniform error responses."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import AppError
from app.routers import devices, telemetry

logger = logging.getLogger("iot.api")


def create_app() -> FastAPI:
    app = FastAPI(
        title="IoT Device Telemetry API",
        description="Local-only device onboarding, credential rotation and "
                    "telemetry ingestion. No external services or real credentials.",
        version="1.0.0",
    )
    app.include_router(devices.router)
    app.include_router(telemetry.router)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        # Stable, machine-readable error envelope; ``code`` is what clients
        # branch on (e.g. credential_expired vs credential_revoked).
        logger.info("api rejection: path=%s code=%s message=%s",
                    request.url.path, exc.code, exc.message)

        body = {"error": {"code": exc.code, "message": exc.message}}
        if exc.detail is not None:
            body["error"]["detail"] = exc.detail
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "storage": "local-memory"}

    return app


app = create_app()
