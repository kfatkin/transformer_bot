import asyncio
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.market import EASTERN
from app.core.models import utcnow
from app.engine.paper_engine import PaperEngine
from app.engine.butterfly_engine import ButterflyEngine
from app.ibkr.client import IBClient


class Scheduler:
    def __init__(
        self, engine: PaperEngine, client: IBClient, butterfly: ButterflyEngine | None = None
    ) -> None:
        self.engine = engine
        self.client = client
        self.butterfly = butterfly
        self.scheduler = AsyncIOScheduler(timezone=EASTERN)

    async def guarded(
        self,
        component: str,
        action: Callable[[], Awaitable[object]],
        *,
        butterfly: ButterflyEngine | None = None,
    ) -> None:
        try:
            await action()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Scheduled task boundary: persist and surface failures; never fake success.
            await (butterfly or self.engine).error(component, exc)

    async def heartbeat(self) -> None:
        await self.guarded("IBKR connection", self.client.connect)
        await self.guarded("market poll", self.engine.poll)
        if self.client.connected:
            await self.guarded("expiration reconciliation", self.engine.settle_due)

    async def scheduled_entry(self) -> None:
        now = utcnow()
        if self.engine.market.is_session(now.astimezone(EASTERN).date()):
            await self.guarded("scheduled entry", self.engine.entry)

    async def preflight(self) -> None:
        await self.guarded("chain preflight", self.engine.prepare)

    async def butterfly_heartbeat(self) -> None:
        if self.butterfly is None:
            return
        await self.guarded("IBKR connection", self.client.connect)
        await self.guarded("0DTE market poll", self.butterfly.poll, butterfly=self.butterfly)
        if self.client.connected:
            await self.guarded(
                "0DTE expiration reconciliation",
                self.butterfly.settle_due,
                butterfly=self.butterfly,
            )

    async def butterfly_entry(self) -> None:
        if self.butterfly is None:
            return
        if not self.butterfly.market.is_session(utcnow().astimezone(EASTERN).date()):
            return
        if not (await self.butterfly.store.butterfly_config()).enabled:
            await self.butterfly.store.log(
                "entry_skip",
                {
                    "strategy": "ZERO_DTE_BUTTERFLY",
                    "timestamp": utcnow().isoformat(),
                    "message": "10:00 0DTE entry disabled by configuration",
                },
            )
            return
        await self.guarded("0DTE scheduled entry", self.butterfly.entry, butterfly=self.butterfly)

    async def close_review(self) -> None:
        now = utcnow()
        day = now.astimezone(EASTERN).date()
        if not self.engine.market.is_session(day):
            return
        _, close = self.engine.market.bounds(day)
        if now >= close:
            await self.engine.store.log(
                "market_close",
                {
                    "session": day.isoformat(),
                    "message": "Session closed. Expired trades await delayed IBKR closing-index proxy or verified PM settlement.",
                },
            )
            await self.engine.csv.export()

    def start(self) -> None:
        defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 5}
        self.scheduler.add_job(
            self.preflight,
            CronTrigger(day_of_week="mon-fri", hour=10, minute=25, timezone=EASTERN),
            id="preflight",
            **defaults,
        )
        self.scheduler.add_job(
            self.scheduled_entry,
            CronTrigger(day_of_week="mon-fri", hour=10, minute=30, timezone=EASTERN),
            id="entry",
            **defaults,
        )
        self.scheduler.add_job(
            self.heartbeat,
            "interval",
            seconds=self.engine.settings.poll_seconds,
            id="poll",
            next_run_time=utcnow(),
            **defaults,
        )
        if self.butterfly is not None:
            self.scheduler.add_job(
                self.butterfly_entry,
                CronTrigger(day_of_week="mon-fri", hour=10, minute=0, timezone=EASTERN),
                id="butterfly-entry",
                **defaults,
            )
            self.scheduler.add_job(
                self.butterfly_heartbeat,
                "interval",
                seconds=self.engine.settings.poll_seconds,
                id="butterfly-poll",
                next_run_time=utcnow(),
                **defaults,
            )
        # Half-day closes are also reviewed; calendar remains the source of truth.
        for hour in (13, 16):
            self.scheduler.add_job(
                self.close_review,
                CronTrigger(day_of_week="mon-fri", hour=hour, minute=1, timezone=EASTERN),
                id=f"close-{hour}",
                **defaults,
            )
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
