from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from factor_service import __version__
from factor_service.api.analysis import router as analysis_router
from factor_service.api.backtests import router as backtests_router
from factor_service.api.factors import router as factors_router
from factor_service.api.formulas import router as formulas_router
from factor_service.api.jobs import router as jobs_router
from factor_service.api.metadata import router as metadata_router
from factor_service.api.models import router as models_router
from factor_service.api.values import router as values_router
from factor_service.clickhouse import init_schema, settings


def create_app() -> FastAPI:
    config = settings()
    app = FastAPI(
        title="AlphaFactorService",
        version=__version__,
        description="Factor definition, compute job, and ClickHouse-backed factor value service.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def startup() -> None:
        init_schema()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(factors_router)
    app.include_router(formulas_router)
    app.include_router(jobs_router)
    app.include_router(analysis_router)
    app.include_router(backtests_router)
    app.include_router(metadata_router)
    app.include_router(models_router)
    app.include_router(values_router)
    return app


app = create_app()


def main() -> None:
    config = settings()
    uvicorn.run(
        "factor_service.main:app",
        host=config.host,
        port=config.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
