"""FastAPI 应用工厂。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.devices import router as devices_router
from .api.telemetry import router as telemetry_router
from .clock import Clock
from .errors import AppError
from .api.dependencies import build_state


def create_app(clock: Clock | None = None) -> FastAPI:
    app = FastAPI(
        title="IoT Device Telemetry API",
        version="0.1.0",
        description="本地内存实现的设备接入与遥测上报服务，无外部依赖。",
    )
    app.state.state = build_state(clock)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code.value,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "request body or parameters failed validation",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(devices_router)
    app.include_router(telemetry_router)
    return app
