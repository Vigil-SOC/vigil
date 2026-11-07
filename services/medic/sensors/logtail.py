"""Log tail: Compose containers and Helm pods as `log` observations.

Compose follows `GET /containers/{id}/logs` through medic-dockerproxy. Helm
follows the pod log API (`follow=true&timestamps=true&sinceTime=`), one stream
per pod. `sinceTime` is whole seconds; the cursor's hashes drop the replayed
part of that second. A container restart is drained once with `previous=true`,
then followed. Neither path sends `limitBytes`.

Background tasks push admitted lines onto their own 15,000-line `Bus` (one 15 s
tick at the admit rate). One `LogStream` sensor per service reports the streams'
state each 30 s, so the scheduler gives it `sensor_health`, `blind` after 3
failed cycles, and "a quiet stream is not an error".

An admitted line takes this path and no other: cap 16 KiB, redact, fingerprint
(fp1), draft. Over 1,000 lines/s on the shared tail, or 500 on one stream, the
line is counted and never redacted or fingerprinted. A raw line is never logged,
stored or put in a `ReadError`.

Cursors live in memory: `{last_ts_ns, hashes of the raw lines at that ns}` per
container or pod. They dedupe an inclusive resume (Docker's `since`, the
kubelet's truncated `sinceTime`) when a stream reattaches, including a line that
was only counted. After Medic itself restarts a stream follows from now; the gap
is the gap mechanism's (`app/gap.py`).
"""

from __future__ import annotations

import asyncio
import calendar
import functools
import hashlib
import json
import logging
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Collection, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from services.medic.contracts.fingerprint_ref import (
    MAX_LINE,
    SERVICES,
    LogLine,
    fingerprint,
    map_level,
    parse_text,
    template_trust,
)
from services.medic.redact import Redactor
from services.medic.sensors.base import Clock, ReadError, Reading, SensorContext, Value
from services.medic.sensors.bus import Bus
from services.medic.sensors.containers import (
    _ID,
    _SERVICE_LABEL,
    _cause,
    _decode,
    _instance,
)
from services.medic.sensors.kube import (
    _CA_PATH,
    _NAMESPACE_PATH,
    _TOKEN_PATH,
    _kube_list,
    _labels,
    _meta,
    _name,
    _read_namespace,
    _read_token,
    _service,
    _stat,
)
from services.medic.sensors.scheduler import iso

log = logging.getLogger("services.medic.sensors")

LOG_SERVICES = SERVICES[:7]  # backend … backup: the containers Vigil ships
HELM_LOG_SERVICES = SERVICES[:5]  # backend … agent-serve; no bifrost, no backup
SIGNAL = {s: "log_" + s.replace("-", "_") for s in LOG_SERVICES}
ERRORS_AS_SUCCESS = "llm_worker_errors_as_success"
JOBS_LOGGER = "services.worker.jobs"
LOG_CAPACITY = 15_000  # one 15 s tick of lines the rate rule admitted
# Measured (#3970, nightly python_json flood, 3 x 5,000 lines/s, Medic already
# following the streams), Medic CPU p95: 1,000/500 -> 1.01; 400/200 -> 1.03;
# 200/100 -> 1.01; 0/0 (nothing admitted) -> 0.92. Reading the streams and counting
# the overflow alone cost ~0.9 vCPU, so no admit rate meets the 0.5 bar; unchanged.
ADMIT_PER_S = 1_000  # the shared tail; the rate rule, not the buffer, bounds load
ADMIT_STREAM_PER_S = 500
OVERFLOW_SIGNAL = "log_overflow"  # sample, not a log signal: counts, no line text
OVERFLOW_EPOCH = "log-overflow"  # in-memory counters; a stable epoch across collects
_WARNING_UP = frozenset({"WARNING", "ERROR", "CRITICAL"})
_WARNING_UP_B = tuple(w.encode() for w in _WARNING_UP)
REDISCOVER_S = 10  # shared by the seven sensors: one container list per cycle
BACKOFF_MAX_S = 30.0
MAX_FRAME = 1 << 20  # a Docker log frame is one message, 16 KiB plus a timestamp
SKEW_MAX_MS = 300_000  # observation.schema.json `t`
_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=None)
_LIST_TIMEOUT = httpx.Timeout(8.0)

# Schema patterns for fields taken from an untrusted line (log $def).
_LOGGER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_EXC_TYPE = re.compile(r"[A-Za-z0-9_.]{1,200}")
_FRAME = re.compile(r"[A-Za-z0-9_./-]{1,200}:[A-Za-z0-9_<>.]{1,100}")
_TRACE = re.compile(r"[0-9a-f]{16,32}")
_TS = re.compile(rb"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))?Z ")
# A pod name goes in the log URL path. DNS-1123 subdomain, no slashes.
_POD = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")


@dataclass(frozen=True)
class Line:
    ts_ns: int | None  # Docker's own timestamp; None if the line had none
    raw: bytes
    truncated: bool = False


# -- the line path ----------------------------------------------------------


def _json_kind(text: str) -> tuple[str, dict[str, Any] | None]:
    """python_json, agent_json, or text. The same gate `line_drafts` uses."""
    if text.startswith("{"):
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if isinstance(doc, dict) and (
            "msg_template" in doc or ("message" in doc and "logger" in doc)
        ):
            return "python_json", doc
        if isinstance(doc, dict) and isinstance(doc.get("msg"), str):
            return "agent_json", doc
    return "text", None


def _parse(
    service: str, text: str, redacted: str, redactor: Redactor
) -> tuple[LogLine, str | None, int]:
    """The format by shape: Python JSON, agent JSON, else plain text. Also trace_id
    and the fields' redaction hits. A JSON line is redacted field by field, on the
    decoded strings: redacting the encoded line misses a secret after an escaped
    newline."""
    hits = 0

    def field(doc: dict[str, Any], key: str) -> str | None:
        nonlocal hits
        value = doc.get(key)
        if not isinstance(value, str):
            return None
        # JSON can carry a lone surrogate (\ud83d); fp1 couldn't hash it.
        red, n = redactor.redact(value.encode("utf-8", "replace").decode())
        hits += n
        return red

    kind, doc = _json_kind(text)
    if kind == "python_json" and doc is not None:
        trace = doc.get("trace_id")
        line = (
            LogLine(
                service,
                "python_json",
                field(doc, "level"),
                field(doc, "logger"),
                field(doc, "msg_template") or None,
                field(doc, "message") or "",
                field(doc, "exc_type"),
                field(doc, "exception"),
            ),
            trace if isinstance(trace, str) and _TRACE.fullmatch(trace) else None,
        )
        return (*line, hits)
    if kind == "agent_json" and doc is not None:
        msg = field(doc, "msg")
        return (
            LogLine(
                service,
                "agent_json",
                field(doc, "level"),
                field(doc, "logger"),
                msg,
                msg,
                field(doc, "error_type"),
            ),
            None,
            hits,
        )
    return LogLine(service, "text", raw=redacted), None, 0


def _template_hash(logger: str, template: str) -> str:
    """Hash of (logger, template) only. The line's text is not an input."""
    return hashlib.sha256(f"{logger}\x1f{template}".encode()).hexdigest()[:16]


def _overflow_bucket(
    service: str, raw: bytes, catalog: Collection[tuple[str, str]]
) -> tuple[str, str | None]:
    """Classify one overflow line without redacting or fingerprinting it.

    Returns ('template', hash), ('level', LEVEL) or ('dropped', None). The
    message lives only for `template_trust`'s f-string check, then is dropped.
    """
    raw = raw[:MAX_LINE]
    # Not JSON and no WARNING+ name in it: parse_text can only read it as INFO.
    if not raw.startswith(b"{") and not any(w in raw for w in _WARNING_UP_B):
        return "dropped", None
    text = raw.decode("utf-8", "replace")
    kind, doc = _json_kind(text)
    if kind == "python_json" and doc is not None:
        logger = doc.get("logger")
        template = doc.get("msg_template")
        message = doc.get("message")
        logger = logger if isinstance(logger, str) else None
        template = template if isinstance(template, str) and template else None
        message = message if isinstance(message, str) else ""
        if logger is not None and not _LOGGER.fullmatch(logger):
            logger = None
        raw_level = doc.get("level")
        level = map_level(raw_level if isinstance(raw_level, str) else None)
        trusted = (
            template_trust(
                LogLine(service, "python_json", level, logger, template, message),
                catalog,
            )
            == "catalog"
        )
        if trusted and template is not None:
            return "template", _template_hash(logger or "", template)
        if level in _WARNING_UP:
            return "level", level
        return "dropped", None
    if kind == "agent_json" and doc is not None:
        raw_level = doc.get("level")
        level = map_level(raw_level if isinstance(raw_level, str) else None)
    else:
        level = map_level(parse_text(LogLine(service, "text", raw=text)).level)
    if level in _WARNING_UP:
        return "level", level
    return "dropped", None


def line_drafts(
    line: Line,
    *,
    service: str,
    instance: str | None,
    shape: str,
    catalog: Collection[tuple[str, str]],
    redactor: Redactor,
    observed_at: float,
    sensor_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """The drafts for one line (two for an llm-worker jobs ERROR) and its `line_hash`."""
    sensor = sensor_id or f"docker.logs.{service}"
    text = line.raw[:MAX_LINE].decode("utf-8", "replace")
    # What `line_hash` covers; text lines parse it.
    redacted, hits = redactor.redact(text)
    source_ts = iso(line.ts_ns / 1e9) if line.ts_ns is not None else None
    line_hash = hashlib.sha256(
        f"{service}|{instance or ''}|{source_ts or ''}|{redacted}".encode()
    ).hexdigest()[:16]

    parsed, trace_id, field_hits = _parse(service, text, redacted, redactor)
    # Only a Python JSON line can be trusted by catalogue; fingerprint.md §2.
    trusted = catalog if parsed.format == "python_json" else ()
    if parsed.logger is not None and not _LOGGER.fullmatch(parsed.logger):
        parsed = replace(parsed, logger=None)
    if parsed.exc_type is not None and not _EXC_TYPE.fullmatch(parsed.exc_type):
        parsed = replace(parsed, exc_type=None)
    fp = fingerprint(parsed, set(trusted))
    shown = parse_text(parsed) if parsed.format == "text" else parsed
    body: dict[str, Any] = {
        "format": parsed.format,
        "level": fp.level,
        "logger": fp.logger or None,
        "template": parsed.template,
        "template_trust": fp.template_trust,
        "message": shown.message or "",
        "args": list(fp.args),
        "exc_type": parsed.exc_type,
        "frames": [f for f in fp.frames if _FRAME.fullmatch(f)],
        "fingerprint": fp.fingerprint,
        "fingerprint_basis": fp.basis,
        "trace_id": trace_id,
        "line_hash": line_hash,
    }
    if line.truncated:
        body["truncated"] = True

    now = iso(observed_at)
    draft: dict[str, Any] = {
        "_emitter": sensor,
        "_subject": sensor,
        # template/message/args come out of this redactor: the choke caps them and
        # adds these hits instead of redacting them again. Never stored.
        "_log_redacted": hits + field_hits,
        "kind": "log",
        "target": {"service": service, "shape": shape},
        "t": now,
        "observed_at": now,
        "source_ts": source_ts,
        "outcome": "ok",
        "log": body,
    }
    if instance is not None:
        draft["target"]["instance"] = instance
    if line.ts_ns is not None:
        skew = round(line.ts_ns / 1e6 - observed_at * 1000)
        draft["skew_ms"] = skew
        draft["skew_flag"] = abs(skew) > SKEW_MAX_MS
        if not draft["skew_flag"]:
            draft["t"] = source_ts
    signals = [SIGNAL[service]]
    if service == "llm-worker" and fp.logger == JOBS_LOGGER and fp.level == "ERROR":
        signals.append(ERRORS_AS_SUCCESS)
    return [{**draft, "signal": s} for s in signals], line_hash


# -- Docker's stream --------------------------------------------------------


class ProtocolError(Exception):
    """A frame that isn't Docker's. The detail would be stream bytes, so none."""


class Reader:
    """Docker's /logs bytes -> whole lines.

    A container without a TTY gets 8-byte frame headers (type, 3 zero bytes, length)
    and one frame per log message, each starting with its timestamp. A message over
    16 KiB arrives as several frames, all but the last without a trailing newline,
    each with its own timestamp: rejoin them under the first. A TTY has no header.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._tty: bool | None = None
        self._part: dict[int, tuple[int | None, bytearray, bool]] = {}

    def feed(self, chunk: bytes) -> Iterator[Line]:
        self._buf += chunk
        if self._tty is None and self._buf:
            self._tty = self._buf[0] not in (0, 1, 2)
        if self._tty:
            cut = self._buf.rfind(b"\n") + 1
            if cut:
                whole, self._buf = bytes(self._buf[:cut]), self._buf[cut:]
                yield from self._frame(
                    1, whole.replace(b"\r\n", b"\n")
                )  # a TTY says \r\n
            if len(self._buf) > MAX_FRAME:
                raise ProtocolError
            return
        # Walk the frames by offset and cut the buffer once per chunk, not per frame.
        data, pos, frames, bad = bytes(self._buf), 0, [], False
        while len(data) - pos >= 8:
            size = int.from_bytes(data[pos + 4 : pos + 8], "big")
            if (
                data[pos] > 2
                or data[pos + 1 : pos + 4] != b"\0\0\0"
                or size > MAX_FRAME
            ):
                bad = True  # the frames before it still go out first
                break
            if len(data) - pos < 8 + size:
                break
            frames.append((data[pos], data[pos + 8 : pos + 8 + size]))
            pos += 8 + size
        if pos:
            del self._buf[:pos]
        for kind, payload in frames:
            yield from self._frame(kind, payload)
        if bad:
            raise ProtocolError

    def _frame(self, kind: int, payload: bytes) -> Iterator[Line]:
        if (
            payload
            and kind not in self._part
            and payload.find(b"\n") == len(payload) - 1
        ):
            # The usual frame: one whole line, nothing held.
            ts_ns, text = _split_ts(payload[:-1])
            yield Line(ts_ns, text[:MAX_LINE], len(text) > MAX_LINE)
            return
        parts = payload.split(b"\n")
        for i, seg in enumerate(parts):
            last = i == len(parts) - 1
            if last and not seg:
                return  # the payload ended on a newline
            ts_ns, text = _split_ts(seg)
            held = self._part.pop(kind, None)
            if held is not None:
                ts_ns, buf, cut = held[0], held[1], held[2]
            else:
                buf, cut = bytearray(), False
            room = MAX_LINE - len(buf)
            cut = cut or len(text) > room
            buf += text[: max(room, 0)]
            if last:
                self._part[kind] = (ts_ns, buf, cut)  # no newline: more follows
            else:
                yield Line(ts_ns, bytes(buf), cut)


def _split_ts(seg: bytes) -> tuple[int | None, bytes]:
    m = _TS.match(seg)
    if m is None:
        return None, seg
    frac = (m.group(7) or b"0").ljust(9, b"0")
    return _epoch_s(seg[:19]) * 1_000_000_000 + int(frac), seg[m.end() :]


@functools.lru_cache(maxsize=64)
def _epoch_s(stamp: bytes) -> int:
    """`YYYY-MM-DDTHH:MM:SS` -> epoch seconds. A stream's lines share a second."""
    y, mo, d = int(stamp[0:4]), int(stamp[5:7]), int(stamp[8:10])
    h, mi, s = int(stamp[11:13]), int(stamp[14:16]), int(stamp[17:19])
    return calendar.timegm((y, mo, d, h, mi, s))


def _since(ts_ns: int) -> str:
    return f"{ts_ns // 1_000_000_000}.{ts_ns % 1_000_000_000:09d}"


def _since_time(ts_ns: int) -> str:
    """RFC3339 whole seconds. The kubelet truncates `sinceTime` to that."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ns // 1_000_000_000))


def _container_name(service: str) -> str:
    """Chart container: `daemon` for soc-daemon, otherwise the component name."""
    return "daemon" if service == "soc-daemon" else service


def _app_container_id(pod: dict[str, Any], container: str) -> str | None:
    status = pod.get("status")
    statuses = status.get("containerStatuses") if isinstance(status, dict) else None
    if not isinstance(statuses, list):
        return None
    for item in statuses:
        if not isinstance(item, dict) or item.get("name") != container:
            continue
        cid = item.get("containerID")
        if isinstance(cid, str) and cid:
            return cid
    return None


def _http_error(status: int) -> ReadError:
    """Status only. A body can echo a pod or the token, so it is not read."""
    if status in (401, 403):
        return ReadError("auth", http_status=status)
    return ReadError("http_status", http_status=status)


class _TextReader:
    """Newline-delimited pod logs (`timestamps=true`), not Docker frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> Iterator[Line]:
        self._buf += chunk
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            seg = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            yield self._line(seg)
        if len(self._buf) > MAX_FRAME:
            raise ProtocolError

    def finish(self) -> Iterator[Line]:
        """A finite body (`previous=true`) may end without a newline."""
        if not self._buf:
            return
        seg = bytes(self._buf)
        self._buf.clear()
        yield self._line(seg)

    def _line(self, seg: bytes) -> Line:
        if seg.endswith(b"\r"):
            seg = seg[:-1]
        ts_ns, text = _split_ts(seg)
        return Line(ts_ns, text[:MAX_LINE], len(text) > MAX_LINE)


def classify(exc: BaseException) -> ReadError:
    """The same classes `containers` uses. Type only: a message can quote bytes."""
    if isinstance(exc, httpx.ConnectTimeout):
        return ReadError("timeout", detail="connect timed out")
    if isinstance(exc, httpx.TimeoutException):
        return ReadError("timeout", detail="timed out")
    if isinstance(exc, httpx.ConnectError):
        if _cause(exc, ConnectionRefusedError):
            return ReadError("refused")
        if _cause(exc, socket.gaierror):
            return ReadError("dns")
        if _cause(exc, ssl.SSLError):
            return ReadError("tls")
    if isinstance(exc, ProtocolError):
        return ReadError("parse")
    return ReadError("other", detail=type(exc).__name__)


class _Failed(Exception):
    def __init__(self, error: ReadError) -> None:
        self.error = error


# -- streams ----------------------------------------------------------------


@dataclass
class _Stream:
    id: str
    name: str | None
    service: str
    last_ns: int  # the cursor: newest timestamp seen …
    # … `hash()` of each raw line at that ns: in-process, as the cursor is
    hashes: set[int] = field(default_factory=set)
    error: ReadError | None = None
    task: asyncio.Task[None] | None = None
    # Helm only. `container_id` is the app container; a change is a restart.
    container: str = ""
    container_id: str | None = None
    pod_uid: str = ""
    need_previous: bool = False
    attached: bool = False  # a follow has started; the next one must re-list
    live: httpx.Response | None = None


@dataclass
class _Counts:
    """Overflow totals for one service. No line text: a template hash, or a level."""

    by_template: dict[str, int] = field(default_factory=dict)
    by_level: dict[str, int] = field(default_factory=dict)
    dropped: int = 0


@dataclass
class _Snapshot:
    error: ReadError | None = None
    streams: dict[str, list[_Stream]] = field(default_factory=dict)  # by service


class LogTail:
    """Discovers the containers and follows each one. Shared by the seven sensors."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        catalog: Collection[tuple[str, str]] = frozenset(),
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.host, self.port = host, port
        # (logger, template) pairs fp1 may trust. Empty until the pack's catalogue
        # is wired (#2731): every line then reads `untrusted`, the safe side.
        self.catalog = catalog
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=_STREAM_TIMEOUT,
            trust_env=False,
            follow_redirects=False,
        )
        self._redactor = redactor or Redactor()
        self._sleep = sleep
        self._streams: dict[str, _Stream] = {}
        self._lock = asyncio.Lock()
        self._snap: _Snapshot | None = None
        self._snap_at = float("-inf")
        self._bus: Bus | None = None
        self._clock: Clock | None = None
        self._shape = "compose"
        self._id_prefix = "docker.logs"
        self._start_ns = 0
        self._counts: dict[str, _Counts] = {}
        self._rate_s: int | None = None
        self._rate_n = 0
        self._rate_stream: dict[str, int] = {}

    def bind(self, bus: Bus, clock: Clock) -> None:
        """The log buffer and the clock; Medic's wiring calls this once."""
        self._bus, self._clock = bus, clock
        self._start_ns = int(clock.wall() * 1e9)  # a stream follows from here

    async def aclose(self) -> None:
        tasks = [s.task for s in self._streams.values() if s.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._streams.clear()
        await self._client.aclose()

    # -- discovery ---------------------------------------------------------

    async def refresh(self, shape: str) -> _Snapshot:
        """The containers now, at most once per REDISCOVER_S however many ask."""
        assert self._clock is not None, "LogTail.bind() was not called"
        async with self._lock:
            now = self._clock.monotonic()
            if self._snap is None or now - self._snap_at >= REDISCOVER_S:
                self._shape = shape
                self._snap = await self._discover()
                self._snap_at = self._clock.monotonic()
            return self._snap

    async def _discover(self) -> _Snapshot:
        url = f"http://{self.host}:{self.port}/containers/json"
        try:
            async with self._client.stream(
                "GET", url, params={"all": "true"}, timeout=_LIST_TIMEOUT
            ) as response:
                body, error = await _decode(response)
        except httpx.HTTPError as exc:
            return _Snapshot(classify(exc))
        if error is not None:
            return _Snapshot(error)
        if not isinstance(body, list):
            return _Snapshot(ReadError("parse"))
        seen: dict[str, _Stream] = {}
        for row in body:
            labels = row.get("Labels") if isinstance(row, dict) else None
            service = labels.get(_SERVICE_LABEL) if isinstance(labels, dict) else None
            cid = row.get("Id") if isinstance(row, dict) else None
            if service not in LOG_SERVICES or not isinstance(cid, str):
                continue
            if not _ID.fullmatch(cid):
                continue
            stream = self._streams.get(cid)
            if stream is None:
                stream = _Stream(
                    cid, _instance(row.get("Names")), service, self._start_ns
                )
                stream.task = asyncio.get_running_loop().create_task(
                    self._run(stream), name=f"medic.logtail.{service}"
                )
            seen[cid] = stream
        for cid in self._streams.keys() - seen.keys():  # removed or recreated
            if task := self._streams[cid].task:
                task.cancel()
        self._streams = seen
        snap = _Snapshot()
        for stream in seen.values():
            snap.streams.setdefault(stream.service, []).append(stream)
        return snap

    # -- following ---------------------------------------------------------

    async def _run(self, stream: _Stream) -> None:
        delay = 1.0
        sleep = self._sleep or self._clock.sleep  # type: ignore[union-attr]
        while True:
            got = False
            try:
                got = await self._attach(stream)
            except asyncio.CancelledError:
                raise
            except _Failed as failed:
                stream.error = failed.error
            except Exception as exc:  # noqa: BLE001
                stream.error = classify(exc)
            # A stream that carried data was healthy: retry soon. One that ends
            # at once (a stopped container) backs off.
            delay = 1.0 if got else min(delay * 2, BACKOFF_MAX_S)
            await sleep(delay)

    async def _attach(self, stream: _Stream) -> bool:
        params = {
            "follow": "1",
            "stdout": "1",
            "stderr": "1",
            "timestamps": "1",
            "since": _since(stream.last_ns),
        }
        url = f"http://{self.host}:{self.port}/containers/{stream.id}/logs"
        base_ns, base_hashes = stream.last_ns, set(stream.hashes)
        reader, got = Reader(), False
        async with self._client.stream("GET", url, params=params) as response:
            if response.status_code != 200:
                raise _Failed(
                    ReadError("http_status", http_status=response.status_code)
                )
            stream.error = None
            try:
                # No chunk size: a fixed one holds a quiet stream's lines back.
                async for chunk in response.aiter_raw():
                    for line in reader.feed(chunk):
                        # Only a new line counts: a stopped container replays its
                        # cursor line and ends, and that must keep backing off.
                        got = self._deliver(stream, line, base_ns, base_hashes) or got
            except httpx.HTTPError:
                pass  # reset or shed mid-stream: reattach from the cursor
        return got

    def _deliver(
        self, stream: _Stream, line: Line, base_ns: int, base_hashes: set[int]
    ) -> bool:
        """Queue one line; False if it was a replay of one already delivered.

        The cursor moves for an overflow line too. Dedupe keeps the builtin `hash()`
        of the raw line (in-process like the cursor, no line text, no digest to pay
        for), so a replay of one that was never fingerprinted is still skipped.
        The observation's `line_hash` stays the redacted one from `line_drafts`.
        """
        assert self._bus is not None and self._clock is not None
        ts = line.ts_ns
        # Docker's `since` is inclusive: a reattach replays the cursor line, and
        # nothing older than the cursor is new.
        if ts is not None and ts < base_ns:
            return False
        if ts is not None:
            raw_hash = hash(line.raw)
            if ts == base_ns and raw_hash in base_hashes:
                return False
            if ts > stream.last_ns:
                stream.last_ns, stream.hashes = ts, {raw_hash}
            elif ts == stream.last_ns:
                stream.hashes.add(raw_hash)
        if self._over_rate(stream):
            self._count_overflow(stream, line)
            return True
        sensor_id = f"{self._id_prefix}.{stream.service}"
        failed: str | None = None
        try:
            drafts, _line_hash = line_drafts(
                line,
                service=stream.service,
                instance=stream.name,
                shape=self._shape,
                catalog=self.catalog,
                redactor=self._redactor,
                observed_at=self._clock.wall(),
                sensor_id=sensor_id,
            )
        # Fail closed: a line that can't be processed is dropped and counted,
        # and the cursor still moves past it so a reattach doesn't replay it.
        except Exception as exc:  # noqa: BLE001
            drafts, failed = [], type(exc).__name__
        if failed is not None:
            self._bus.stats[sensor_id].redactor_failures += 1
            log.warning(
                "Dropped a %s log line: processing failed (%s)", stream.service, failed
            )
        for draft in drafts:
            # Unbound: a Helm tail has no `_enqueue` of its own.
            LogTail._enqueue(self, draft)
        return True

    def _over_rate(self, stream: _Stream) -> bool:
        """True when this line is past 1,000/s on the tail or 500/s on the stream."""
        assert self._clock is not None
        second = int(self._clock.monotonic())
        if second != self._rate_s:
            self._rate_s = second
            self._rate_n = 0
            self._rate_stream.clear()
        n = self._rate_stream.get(stream.id, 0)
        if self._rate_n >= ADMIT_PER_S or n >= ADMIT_STREAM_PER_S:
            return True
        self._rate_stream[stream.id] = n + 1
        self._rate_n += 1
        return False

    def _count_overflow(self, stream: _Stream, line: Line) -> None:
        """Count the line. Every overflow increments the sensor's `dropped`."""
        assert self._bus is not None
        try:
            bucket, key = _overflow_bucket(stream.service, line.raw, self.catalog)
        except Exception:  # noqa: BLE001 — the line stays out of the log
            bucket, key = "dropped", None
        counts = self._counts.setdefault(stream.service, _Counts())
        if bucket == "template" and key is not None:
            counts.by_template[key] = counts.by_template.get(key, 0) + 1
        elif bucket == "level" and key is not None:
            counts.by_level[key] = counts.by_level.get(key, 0) + 1
        else:
            counts.dropped += 1
        self._bus.stats[f"{self._id_prefix}.{stream.service}"].dropped += 1

    def overflow_reading(self, service: str) -> Reading:
        """Cumulative overflow counters for one stream. No line text."""
        counts = self._counts.get(service)
        labels = {"stream": service}
        values = [
            Value.counter("dropped", 0 if counts is None else counts.dropped, labels)
        ]
        if counts is not None:
            for level in sorted(counts.by_level):
                values.append(
                    Value.counter(
                        "by_level",
                        counts.by_level[level],
                        {"stream": service, "level": level},
                    )
                )
            for digest in sorted(counts.by_template):
                values.append(
                    Value.counter(
                        "by_template",
                        counts.by_template[digest],
                        {"stream": service, "template": digest},
                    )
                )
        return Reading(OVERFLOW_SIGNAL, service, values=values, epoch=OVERFLOW_EPOCH)

    def _enqueue(self, draft: dict[str, Any]) -> None:
        """Backstop for a late drain: the oldest line goes and is counted.
        `_deliver` is what refuses a line before it is redacted."""
        assert self._bus is not None
        self._bus.put(draft)


# -- Helm pods --------------------------------------------------------------


class KubeLogTail:
    """Follows Helm pod logs. One cursor per pod, from Medic's start.

    Discovery is the pod list, on the same rediscover interval as Compose.
    A new app-container `containerID` while attached is a restart: drain
    `previous=true` once (finite, no `follow`), then follow the new container.
    No `limitBytes` and no kubelet-retention backfill.
    """

    def __init__(
        self,
        *,
        host: str = "kubernetes.default.svc",
        port: int = 443,
        scheme: str = "https",
        token_path: Path = _TOKEN_PATH,
        ca_path: Path = _CA_PATH,
        namespace_path: Path = _NAMESPACE_PATH,
        catalog: Collection[tuple[str, str]] = frozenset(),
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.host, self.port, self.scheme = host, port, scheme
        self.token_path, self.ca_path = token_path, ca_path
        self.namespace_path = namespace_path
        self.catalog = catalog
        self._transport = transport
        # Opened on the first list. `verify=<ca path>` raises if the mount is
        # not there yet, and `sensors_for` must not do that at registration.
        self._client: httpx.AsyncClient | None = None
        self._redactor = redactor or Redactor()
        self._sleep = sleep
        self._streams: dict[str, _Stream] = {}
        self._lock = asyncio.Lock()
        self._snap: _Snapshot | None = None
        self._snap_at = float("-inf")
        self._dropped: list[_Stream] = []
        self._bus: Bus | None = None
        self._clock: Clock | None = None
        self._shape = "helm"
        self._id_prefix = "kube.logs"
        self._start_ns = 0

    def bind(self, bus: Bus, clock: Clock) -> None:
        self._bus, self._clock = bus, clock
        self._start_ns = int(clock.wall() * 1e9)

    async def aclose(self) -> None:
        tasks = [s.task for s in self._streams.values() if s.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._streams.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _open(self) -> httpx.AsyncClient | ReadError:
        if self._client is not None:
            return self._client
        if self.scheme == "https":
            missing = await _stat(self.ca_path)
            if missing is not None:
                return missing
        verify: bool | str = str(self.ca_path) if self.scheme == "https" else False
        try:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=_STREAM_TIMEOUT,
                trust_env=False,
                follow_redirects=False,
                verify=verify,
            )
        except (OSError, ssl.SSLError):
            return ReadError("tls")
        return self._client

    async def refresh(self, shape: str) -> _Snapshot:
        """The pods now, at most once per REDISCOVER_S however many ask."""
        assert self._clock is not None, "KubeLogTail.bind() was not called"
        return await self._reload(force=False, shape=shape)

    async def _reload(self, *, force: bool, shape: str | None = None) -> _Snapshot:
        assert self._clock is not None
        async with self._lock:
            if shape is not None:
                self._shape = shape
            now = self._clock.monotonic()
            if force or self._snap is None or now - self._snap_at >= REDISCOVER_S:
                self._snap = await self._discover()
                self._snap_at = self._clock.monotonic()
                dropped, self._dropped = self._dropped, []
            else:
                dropped = []
            snap = self._snap
        # Outside the lock: closing the socket unblocks that follow task, which
        # then drains `previous` itself. A list from inside `_attach` has no
        # live response of its own.
        for stream in dropped:
            await self._drop_follow(stream)
        assert snap is not None
        return snap

    async def _discover(self) -> _Snapshot:
        client = await self._open()
        if isinstance(client, ReadError):
            return _Snapshot(client)
        namespace, error = await _read_namespace(self.namespace_path)
        if error is not None or namespace is None:
            return _Snapshot(error or ReadError("parse", detail="namespace"))
        url = f"{self.scheme}://{self.host}:{self.port}/api/v1/namespaces/{namespace}/pods"
        listed = await _kube_list(
            client,
            url,
            token_path=self.token_path,
            deadline=time.monotonic() + 8,
        )
        if isinstance(listed, ReadError):
            return _Snapshot(listed)
        seen: dict[str, _Stream] = {}
        for pod in listed:
            service = _service(_labels(pod))
            pod_name = _name(pod)
            if service not in HELM_LOG_SERVICES or pod_name is None:
                continue
            if not _POD.fullmatch(pod_name):
                continue
            uid = _meta(pod).get("uid")
            pod_uid = uid if isinstance(uid, str) and uid else pod_name
            container = _container_name(service)
            cid = _app_container_id(pod, container)
            stream = self._streams.get(pod_name)
            if stream is None or stream.pod_uid != pod_uid:
                if stream is not None and stream.task:
                    stream.task.cancel()
                stream = _Stream(
                    pod_name,
                    pod_name,
                    service,
                    self._start_ns,
                    container=container,
                    container_id=cid,
                    pod_uid=pod_uid,
                )
                stream.task = asyncio.get_running_loop().create_task(
                    self._run(stream), name=f"medic.kube.logs.{service}"
                )
            elif cid and stream.container_id and cid != stream.container_id:
                stream.need_previous = True
                stream.container_id = cid
                self._dropped.append(stream)
            elif cid:
                stream.container_id = cid
            seen[pod_name] = stream
        for pod_name in self._streams.keys() - seen.keys():
            if task := self._streams[pod_name].task:
                task.cancel()
        self._streams = seen
        snap = _Snapshot()
        for stream in seen.values():
            snap.streams.setdefault(stream.service, []).append(stream)
        return snap

    async def _drop_follow(self, stream: _Stream) -> None:
        live = stream.live
        if live is None:
            return
        try:
            await live.aclose()
        except Exception as exc:  # noqa: BLE001 — the follow task sees the close
            log.debug("pod log follow close failed (%s)", type(exc).__name__)

    async def _run(self, stream: _Stream) -> None:
        delay = 1.0
        sleep = self._sleep or self._clock.sleep  # type: ignore[union-attr]
        while True:
            got = False
            try:
                got = await self._attach(stream)
            except asyncio.CancelledError:
                raise
            except _Failed as failed:
                stream.error = failed.error
            except Exception as exc:  # noqa: BLE001
                stream.error = classify(exc)
            if stream.need_previous:
                continue  # the container changed: drain, don't back off
            delay = 1.0 if got else min(delay * 2, BACKOFF_MAX_S)
            await sleep(delay)

    async def _attach(self, stream: _Stream) -> bool:
        # A follow that ended (the kubelet closes it when the container dies)
        # must see the new containerID before it reads again. Otherwise the
        # new container's lines move the cursor and `previous=true` skips what
        # the old container still had. The open follow itself stays on the
        # shared rediscover interval.
        if stream.attached:
            snap = await self._reload(force=True)
            # A failed list must not be followed by a read: the container may
            # have restarted, and the new container's lines would move the
            # cursor past the old container's tail.
            if snap.error is not None and not stream.need_previous:
                stream.error = snap.error
                return False
        stream.attached = True
        # One restart, one finite read of the container we were following.
        # A further change while that read is open sets the flag again.
        while stream.need_previous:
            stream.need_previous = False
            try:
                await self._read_log(stream, follow=False, previous=True)
            except _Failed as failed:
                stream.error = failed.error
            except Exception as exc:  # noqa: BLE001
                stream.error = classify(exc)
        return await self._read_log(stream, follow=True, previous=False)

    async def _read_log(self, stream: _Stream, *, follow: bool, previous: bool) -> bool:
        token, error = await _read_token(self.token_path)
        if error is not None or token is None:
            raise _Failed(error or ReadError("other", detail="token"))
        namespace, error = await _read_namespace(self.namespace_path)
        if error is not None or namespace is None:
            raise _Failed(error or ReadError("parse", detail="namespace"))
        # Never `limitBytes`: it would resume mid-line and skip the rest.
        params = {
            "container": stream.container,
            "timestamps": "true",
            "sinceTime": _since_time(stream.last_ns),
        }
        if follow:
            params["follow"] = "true"
        if previous:
            params["previous"] = "true"
        url = (
            f"{self.scheme}://{self.host}:{self.port}"
            f"/api/v1/namespaces/{namespace}/pods/{quote(stream.id, safe='')}/log"
        )
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/plain"}
        base_ns, base_hashes = stream.last_ns, set(stream.hashes)
        reader, got = _TextReader(), False
        client = self._client
        if client is None:
            opened = await self._open()
            if isinstance(opened, ReadError):
                raise _Failed(opened)
            client = opened
        async with client.stream(
            "GET", url, params=params, headers=headers
        ) as response:
            if response.status_code != 200:
                raise _Failed(_http_error(response.status_code))
            stream.error = None
            if follow:
                stream.live = response
            try:
                async for chunk in response.aiter_raw():
                    for line in reader.feed(chunk):
                        got = (
                            LogTail._deliver(self, stream, line, base_ns, base_hashes)
                            or got
                        )
                    if follow and stream.need_previous:
                        return got
                if not follow:
                    for line in reader.finish():
                        got = (
                            LogTail._deliver(self, stream, line, base_ns, base_hashes)
                            or got
                        )
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError:
                if not follow:
                    raise  # a short body that died is a failed drain; reattach follows
            except Exception:
                # Closing the follow socket on restart is not an HTTP error.
                if not follow or not stream.need_previous:
                    raise
            finally:
                if stream.live is response:
                    stream.live = None
        return got


# -- the sensors ------------------------------------------------------------


@dataclass
class LogStream:
    """One service's log signal. Reads nothing itself: it reports its streams."""

    tail: LogTail | KubeLogTail
    service: str
    interval_s: int = 30  # C7 §3
    timeout_s: float = 10.0
    uses_vigil_api: bool = False
    id_prefix: str = "docker.logs"
    id: str = field(init=False)
    covers: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        self.id = f"{self.id_prefix}.{self.service}"
        self.covers = (SIGNAL[self.service], OVERFLOW_SIGNAL) + (
            (ERRORS_AS_SUCCESS,) if self.service == "llm-worker" else ()
        )

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        snap = await self.tail.refresh(ctx.shape)
        if snap.error is not None:
            return self._errors(snap.error)
        streams = snap.streams.get(self.service)
        if not streams:  # the daemon and llm-worker are optional
            return self._errors(ReadError("not_installed"))
        errors = [s.error for s in streams]
        if all(e is not None for e in errors):
            return self._errors(errors[0], streams[0].name)
        # One ok reading beside the errors would keep a failed cycle from
        # counting as failed (the scheduler fails only when every reading is).
        return [self.tail.overflow_reading(self.service)]

    def _errors(self, error: ReadError | None, instance: str | None = None):
        return [
            Reading(s, self.service, error=error, instance=instance)
            for s in self.covers
        ]


def compose_log_sensors(
    host: str, port: int, *, catalog: Collection[tuple[str, str]] = frozenset()
) -> list[LogStream]:
    tail = LogTail(host, port, catalog=catalog)
    return [LogStream(tail, service) for service in LOG_SERVICES]


def helm_log_sensors(
    *,
    catalog: Collection[tuple[str, str]] = frozenset(),
    host: str = "kubernetes.default.svc",
    port: int = 443,
    scheme: str = "https",
    token_path: Path = _TOKEN_PATH,
    ca_path: Path = _CA_PATH,
    namespace_path: Path = _NAMESPACE_PATH,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[LogStream]:
    """The five Helm log streams. `catalog` is the argument Compose already takes."""
    tail = KubeLogTail(
        host=host,
        port=port,
        scheme=scheme,
        token_path=token_path,
        ca_path=ca_path,
        namespace_path=namespace_path,
        catalog=catalog,
        transport=transport,
    )
    return [
        LogStream(tail, service, id_prefix="kube.logs") for service in HELM_LOG_SERVICES
    ]


def bind_log_tails(sensors: Sequence[Any], bus: Bus, clock: Clock) -> None:
    """Hand each log sensor's tail the log buffer. Called by whoever owns the bus."""
    for tail in {s.tail for s in sensors if isinstance(s, LogStream)}:
        tail.bind(bus, clock)
