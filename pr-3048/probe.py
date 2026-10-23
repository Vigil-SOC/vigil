import sys; sys.path.insert(0, ".")
from unittest.mock import patch
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
from opentelemetry.trace import Status, StatusCode
import core.platform.monitoring as m
assert issubclass(m.SentrySpanProcessor, SpanProcessor)
assert not hasattr(m, "add_breadcrumb")
assert "on_shutdown" not in vars(m.SentrySpanProcessor) and "force_flush" not in vars(m.SentrySpanProcessor)
calls = []
p = m.SentrySpanProcessor()
for h in ("on_start", "on_end", "shutdown", "force_flush"):
    o = getattr(p, h)
    setattr(p, h, lambda *a, _h=h, _o=o, **k: (calls.append(_h), _o(*a, **k))[1])
tp = TracerProvider(); tp.add_span_processor(p)
with patch.object(m.sentry_sdk, "add_breadcrumb") as bc, patch.object(m.sentry_sdk, "set_tag") as tag:
    with tp.get_tracer("x").start_as_current_span("ok"): pass
    with tp.get_tracer("x").start_as_current_span("bad") as s: s.set_status(Status(StatusCode.ERROR))
    print("force_flush ->", tp.force_flush()); tp.shutdown()
    print("breadcrumbs:", [c.kwargs["message"] for c in bc.call_args_list], "set_tag calls:", tag.call_count)
    assert [c.kwargs["message"] for c in bc.call_args_list] == ["bad"]
print("calls:", calls)
# telemetry wiring with a DSN set
import core.telemetry as t
from core.config import get_settings
st = get_settings()
with patch.object(type(st), "sentry_dsn", "https://k@example.invalid/1", create=True):
    pass
