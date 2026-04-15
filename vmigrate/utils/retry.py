"""Retry decorator with exponential backoff for vmigrate.

Wraps any callable so that transient errors are automatically retried with
increasing delays.  Each attempt is logged so that operators can trace what
went wrong and how many retries were consumed.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Callable, Tuple, Type

logger = logging.getLogger("vmigrate.retry")


def retry(
    attempts: int = 3,
    delay: int = 30,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable:
    """Decorator that retries a function on failure with exponential backoff.

    The delay between retries doubles on each attempt (exponential backoff):
    - Attempt 1 fails  → wait ``delay`` seconds
    - Attempt 2 fails  → wait ``delay * 2`` seconds
    - Attempt 3 fails  → raise the last exception

    Args:
        attempts: Maximum number of attempts (including the first call).
            Must be >= 1.
        delay: Base delay in seconds between retries.  Actual delay is
            ``delay * 2^(attempt_number - 1)``.
        exceptions: Tuple of exception types to catch and retry on.  Any
            other exception will propagate immediately.

    Returns:
        A decorator that wraps the target function.

    Example::

        @retry(attempts=3, delay=10, exceptions=(IOError, TimeoutError))
        def upload_disk(path):
            ...
    """
    if attempts < 1:
        raise ValueError("retry: attempts must be >= 1")

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            last_exc: Exception | None = None
            for attempt_num in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # type: ignore[misc]
                    last_exc = exc
                    if attempt_num == attempts:
                        logger.error(
                            "retry: %s failed after %d attempt(s). "
                            "Last error: %s",
                            func.__qualname__,
                            attempts,
                            exc,
                        )
                        raise
                    wait = delay * (2 ** (attempt_num - 1))
                    logger.warning(
                        "retry: %s attempt %d/%d failed with %s: %s. "
                        "Retrying in %d seconds...",
                        func.__qualname__,
                        attempt_num,
                        attempts,
                        type(exc).__name__,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
            # Should never reach here
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator
