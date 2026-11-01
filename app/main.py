from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.tool_queue import get_tool_queue_worker
from app.services.observability import ObservationMiddleware, configure_logging, observation_scope
from app.services.model_gateway import bind_ledger, persist_run
from app.services.turn_execution import TurnRuntime
from app.services.turn_middleware import TurnBudgetMiddleware
from app.core.privacy_crypto import PrivacyStorageError
from fastapi.responses import JSONResponse


def create_app() -> FastAPI:
    configure_logging(get_settings())
    app = FastAPI(title="MindBridge Python", version="0.1.0")
    app.state.turn_runtime = TurnRuntime(get_settings())

    @app.exception_handler(PrivacyStorageError)
    async def unavailable_sensitive_storage(request, exc):
        return JSONResponse(status_code=503,content={'detail':'敏感数据暂不可用，请联系系统维护人员。'})

    @app.middleware("http")
    async def no_cache_frontend_assets(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith('/api/') or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-store"
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @app.on_event("startup")
    def startup() -> None:
        create_schema()
        with observation_scope(ensure_run=True, reuse=False) as observation:
            bind_ledger(observation, SessionLocal, scope='knowledge_index')
            with SessionLocal() as db:
                try:
                    seed_data(db)
                finally:
                    persist_run(observation, finalized=True)
        from app.services.memory import RedisShortTermMemoryStore
        RedisShortTermMemoryStore(get_settings()).upgrade_legacy_cache()
        worker = get_tool_queue_worker(get_settings())
        worker.start()
        app.state.tool_queue_worker = worker

    @app.on_event("shutdown")
    def shutdown() -> None:
        app.state.turn_runtime.close()
        worker = getattr(app.state, "tool_queue_worker", None)
        if worker is not None:
            worker.stop()

    app.add_middleware(TurnBudgetMiddleware, settings=get_settings(), runtime=app.state.turn_runtime)
    app.add_middleware(ObservationMiddleware, session_factory=SessionLocal)
    app.include_router(router)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()
