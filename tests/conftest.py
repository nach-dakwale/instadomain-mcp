"""Shared test helpers."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock


@asynccontextmanager
async def noop_lifespan(_app):
    yield


class MockConn:
    def __init__(self, fetchrow_return=None):
        self.execute = AsyncMock()
        self.fetchrow = AsyncMock(return_value=fetchrow_return)


class MockPool:
    def __init__(self, conn=None):
        self.conn = conn or MockConn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn
