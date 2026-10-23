import sys; sys.path.insert(0, ".")
import core.telemetry as t
from core.config import get_settings
getattr(get_settings, "cache_clear", lambda: None)()
print("dsn set:", bool(get_settings().sentry_dsn))
t._do_init("probe")
procs = [type(p).__name__ for p in t._tracer_provider._active_span_processor._span_processors]
print("processors:", procs)
assert "SentrySpanProcessor" in procs
