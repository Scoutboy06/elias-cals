"""HTTP boundary, safe TimeEdit URL resolution, caching, and ICS transformation."""

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from icalendar import Calendar

import transforms

logger = logging.getLogger(__name__)
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_CACHED_FEEDS = 128
FEED_PATH = re.compile(r"/([A-Za-z0-9_-]+)/web/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)\.ics")
FEED_ID = re.compile(r"([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)")
TENANT = re.compile(r"[A-Za-z0-9_-]+")


def source_url(feed_id: str | None, url: str | None, tenant: str) -> str:
    if (feed_id is None) == (url is None):
        raise HTTPException(400, "Supply exactly one of 'id' or 'url'.")
    if url is not None:
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise HTTPException(400, "Invalid TimeEdit URL.") from None
        if (
            parsed.scheme != "https"
            or parsed.netloc != "cloud.timeedit.net"
            or not FEED_PATH.fullmatch(parsed.path)
            or parsed.query
            or parsed.fragment
        ):
            raise HTTPException(
                400,
                "Expected https://cloud.timeedit.net/<tenant>/web/<view>/<feed>.ics",
            )
        return url
    match = FEED_ID.fullmatch(feed_id or "")
    if not match or not TENANT.fullmatch(tenant):
        raise HTTPException(400, "Expected id=<view>.<feed> and a valid tenant name.")
    view, feed = match.groups()
    return f"https://cloud.timeedit.net/{tenant}/web/{view}/{feed}.ics"


def parse_calendar(data: bytes) -> Calendar:
    calendar = Calendar.from_ical(data)
    if calendar.name != "VCALENDAR" or not calendar.get("VERSION"):
        raise ValueError("Not an iCalendar feed")
    return calendar


def adapt(data: bytes) -> bytes:
    calendar = parse_calendar(data)

    def visit(component):
        children = []
        for child in component.subcomponents:
            if child.name == "VEVENT":
                child = transforms.transform_event(child)
                if child is None:
                    continue
            else:
                visit(child)
            children.append(child)
        component.subcomponents = children

    visit(calendar)
    return transforms.transform_calendar(calendar).to_ical()


@dataclass
class CachedFeed:
    data: bytes
    fetched_at: float


class FeedCache:
    def __init__(self, client: httpx.AsyncClient, ttl: float, stale_ttl: float):
        self.client = client
        self.ttl = ttl
        self.stale_ttl = stale_ttl
        self.entries: OrderedDict[str, CachedFeed] = OrderedDict()
        # A single lock keeps this small service bounded and prevents duplicate fetches.
        self.lock = asyncio.Lock()

    async def get(self, url: str) -> tuple[bytes, bool]:
        async with self.lock:
            cached = self.entries.get(url)
            now = time.monotonic()
            if cached:
                self.entries.move_to_end(url)
                if now - cached.fetched_at < self.ttl:
                    return cached.data, False
            try:
                async with self.client.stream("GET", url) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_FEED_BYTES:
                            raise ValueError("Feed exceeds size limit")
                result = bytes(data)
                parse_calendar(result)  # Never cache invalid upstream responses.
            except (httpx.HTTPError, ValueError) as exc:
                # Do not log private source URLs or response bodies.
                logger.warning("TimeEdit fetch failed (%s)", type(exc).__name__)
                if cached and now - cached.fetched_at < self.stale_ttl:
                    return cached.data, True
                raise HTTPException(502, "TimeEdit feed unavailable or invalid.") from None
            self.entries[url] = CachedFeed(result, time.monotonic())
            while len(self.entries) > MAX_CACHED_FEEDS:
                self.entries.popitem(last=False)
            return result, False


def create_app(*, upstream_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=False,
            transport=upstream_transport,
            headers={"User-Agent": "timeedit-adapter/0.1", "Accept": "text/calendar"},
        ) as client:
            app.state.feeds = FeedCache(
                client,
                ttl=max(0, float(os.getenv("CACHE_TTL_SECONDS", "600"))),
                stale_ttl=max(0, float(os.getenv("STALE_TTL_SECONDS", "86400"))),
            )
            yield

    app = FastAPI(title="TimeEdit calendar adapter", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/adapter/timeedit")
    async def timeedit(
        request: Request,
        id: str | None = Query(None, max_length=512),
        url: str | None = Query(None, max_length=2048),
        tenant: str = Query("chalmers", max_length=100),
    ):
        upstream = source_url(id, url, tenant)
        data, stale = await request.app.state.feeds.get(upstream)
        try:
            output = adapt(data)
        except Exception:
            logger.exception("Calendar transformation failed")
            raise HTTPException(
                500, "Calendar transformation failed; check transforms.py."
            ) from None
        return Response(
            content=output,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Cache-Control": "private, no-cache",
                "X-Feed-Stale": str(stale).lower(),
                "Content-Disposition": 'inline; filename="calendar.ics"',
            },
        )

    return app


app = create_app()
