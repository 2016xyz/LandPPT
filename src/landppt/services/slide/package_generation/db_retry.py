"""Retry only database conflicts known to have rolled back the transaction."""

import asyncio
import random
from functools import wraps

from sqlalchemy.exc import DBAPIError


def retry_transaction(operation):
    @wraps(operation)
    async def wrapped(*args, **kwargs):
        for attempt in range(4):
            try:
                return await operation(*args, **kwargs)
            except DBAPIError as exc:
                original = exc.orig
                code = getattr(original, "sqlstate", None) or getattr(
                    original, "pgcode", None
                )
                sqlite_code = getattr(original, "sqlite_errorcode", 0) or 0
                message = str(original).lower()
                retryable = (
                    code in {"40001", "40P01", "55P03"}
                    or (sqlite_code & 255) in {5, 6}
                    or message
                    in {
                        "database is locked",
                        "database table is locked",
                        "database is busy",
                    }
                )
                if not retryable or attempt == 3:
                    raise
                await asyncio.sleep(0.1 * 2**attempt + random.uniform(0, 0.1))

    return wrapped
