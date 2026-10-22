import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.advisor.ai_agent import Advisor
from app.api.routes import router
from app.core.config import Settings
from app.core.market import MarketCalendar
from app.core.store import Store
from app.engine.csv_logger import CSVLogger
from app.engine.butterfly_engine import ButterflyEngine
from app.engine.paper_engine import PaperEngine
from app.engine.scheduler import Scheduler
from app.ibkr.chains import ButterflySelector, ChainSelector
from app.ibkr.client import IBClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(settings: Settings | None = None, run_scheduler: bool = True) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.data_dir)
        await store.initialize()
        ib = IBClient(settings)
        market = MarketCalendar()
        csv = CSVLogger(store, settings.data_dir / "csv")
        advisor = Advisor(settings, store)
        engine = PaperEngine(store, ib, ChainSelector(ib, market), market, csv, advisor, settings)
        butterfly = ButterflyEngine(
            store, ib, ButterflySelector(ib), market, csv, advisor, settings
        )
        app.state.engine = engine
        app.state.butterfly = butterfly
        app.state.ib = ib
        await csv.export()
        worker = asyncio.create_task(advisor.run(), name="advisor")
        scheduler = Scheduler(engine, ib, butterfly)
        if run_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            if run_scheduler:
                scheduler.stop()
            # APScheduler cancels running async jobs; wait for the trading lock
            # before closing connections used by any in-flight transaction.
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            async with engine.entry_lock, engine.lock, butterfly.entry_lock, butterfly.lock:
                ib.disconnect()
                await store.engine.dispose()

    app = FastAPI(title="DC Time Machine · SPX Paper Lab", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    {"detail": "Cross-origin writes are not allowed"}, status_code=403
                )
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "Use application/json"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    app.include_router(router)
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    app.mount("/", StaticFiles(directory=frontend, html=True), name="dashboard")
    return app


app = create_app()
