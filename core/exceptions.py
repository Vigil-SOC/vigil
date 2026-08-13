import functools
import inspect
import logging


class SOCError(Exception):
    """Base for expected domain failures.

    The API renders these directly: ``message`` reaches the client, so keep it
    free of internal detail. Subclasses set ``status_code`` to choose the HTTP
    status; anything else surfaces as a 500.
    """

    status_code: int = 500

    def __init__(self, message: str, code: str = "SOC_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class DatabaseError(SOCError):
    def __init__(self, message: str):
        super().__init__(message, "DATABASE_ERROR")


def default_on_error(default):
    """Log and return ``default`` if the wrapped function raises.

    For service methods whose callers read a falsy result as "unavailable".
    Pass a factory (``list``, ``dict``) for mutable defaults so callers cannot
    mutate a shared instance. Do not use where the caller needs to tell failure
    apart from a legitimately empty result — raise a `SOCError` there instead.

    Sync functions only: the wrapper cannot await, so decorating a coroutine
    would catch nothing. That raises at decoration time rather than silently.
    """

    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            raise TypeError(f"{fn.__qualname__}: default_on_error is sync-only")

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                logging.getLogger(fn.__module__).exception("%s failed", fn.__qualname__)
                return default() if callable(default) else default

        return wrapper

    return decorate
