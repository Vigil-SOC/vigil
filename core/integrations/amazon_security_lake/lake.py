"""Amazon Security Lake over Athena: discovery, the SQL check, one query per call.

Read-only. Discovery reads the Glue catalogue shared to the query access and
never calls a ``securitylake:*`` API. A query runs in one region's Athena with
the operator's workgroup. Whether its rows come back is decided in one place,
``answer``: a region not in ``allowed_regions`` answers a pointer (row count,
bytes scanned) and ``GetQueryResults`` is never called for it.

No error text here may contain "timeout" or "timed out": ``mcp_tools._failure_kind``
reads that wording as a ``timeout`` and drops the region and query id with it.
botocore's own messages can, so exceptions are named by type, never echoed.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.credentials import (
    AssumeRoleCredentialFetcher,
    DeferredRefreshableCredentials,
)
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)

# A little under the 120 s telemetry_search bound, as splunk/client.SEARCH_DEADLINE_S.
SEARCH_DEADLINE_S = 110.0
# Kept back from polling for the results read, so the answer lands inside the bound.
_FETCH_RESERVE_S = 10.0
MAX_RESULTS = 500

DB_PREFIX = "amazon_security_lake_glue_db"
# amazon_security_lake_table_<region>_<source>_<major>_<minor>; custom sources carry
# no version suffix.
_TABLE = re.compile(
    r"^amazon_security_lake_table_(?P<region>[a-z]{2}(?:_[a-z]+)+_\d+)_"
    r"(?P<source>[a-z0-9_]+?)(?:_(?P<major>\d+)_(?P<minor>\d+))?$"
)
# Security Lake source version -> the OCSF version its tables carry.
_OCSF = {"1": "1.0.0-rc.2", "2": "1.1.0"}

READABLE, REFUSED, ABSENT, UNREACHABLE = "readable", "refused", "absent", "unreachable"
_DENIED = {"AccessDeniedException", "AccessDenied", "UnauthorizedOperation"}
# What Glue answers in an opt-in region the account has not enabled.
_NOT_ENABLED = {"UnrecognizedClientException", "InvalidSignatureException"}

_DISCOVERY_CONFIG = Config(
    connect_timeout=2, read_timeout=4, retries={"total_max_attempts": 1}
)
_QUERY_CONFIG = Config(
    connect_timeout=3, read_timeout=8, retries={"total_max_attempts": 2}
)

_STS_CONFIG = Config(
    connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2}
)
# A boto3 Session is not thread-safe while it builds clients.
_client_lock = threading.Lock()

_TIME_WORDS = re.compile(r"time[sd]?\s*-?\s*out", re.IGNORECASE)
# Not a UUID group: a query id ending in twelve digits must survive.
_ACCOUNT_ID = re.compile(r"(?<![\w-])\d{12}(?![\w-])")


class LakeError(Exception):
    """A handled failure. Its message is built here, never from AWS text."""


def scrub(text: str) -> str:
    """Strip the words _failure_kind reads as a timeout, and any account id."""
    return _ACCOUNT_ID.sub("<account>", _TIME_WORDS.sub("deadline", text))


def describe_exc(exc: BaseException) -> str:
    """An exception as something safe to show: its AWS code or its type, never its text."""
    if isinstance(exc, ClientError):
        return (exc.response.get("Error") or {}).get("Code") or "ClientError"
    if isinstance(exc, NoCredentialsError):
        return "no AWS credentials found"
    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError)):
        # Their class names say "Timeout", which _failure_kind would read.
        return "the endpoint did not answer in time"
    if isinstance(exc, EndpointConnectionError):
        return "could not connect to the endpoint"
    return scrub(type(exc).__name__)


def parse_regions(value: Any) -> Tuple[str, ...]:
    return tuple(r.strip().lower() for r in str(value or "").split(",") if r.strip())


# ------------------------------------------------------------------ credentials


def build_session(config: Mapping[str, Any]) -> boto3.Session:
    """The default chain of this process, assuming ``role_arn`` when one is set."""
    base = boto3.Session()
    role_arn = config.get("role_arn")
    if not role_arn:
        return base
    region = base.region_name or "us-east-1"
    extra: Dict[str, Any] = {"RoleSessionName": "vigil-security-lake"}
    if config.get("external_id"):
        extra["ExternalId"] = config["external_id"]
    source = base.get_credentials()
    if source is None:
        raise NoCredentialsError()
    fetcher = AssumeRoleCredentialFetcher(
        client_creator=lambda svc, **kw: base.client(
            svc, region_name=region, config=_STS_CONFIG, **kw
        ),
        source_credentials=source,
        role_arn=role_arn,
        extra_args=extra,
    )
    botocore_session = base._session  # noqa: SLF001 - the documented refresh hook
    botocore_session._credentials = DeferredRefreshableCredentials(  # noqa: SLF001
        refresh_using=fetcher.fetch_credentials, method="assume-role"
    )
    return boto3.Session(botocore_session=botocore_session)


def client(session: boto3.Session, service: str, region: str, config: Config):
    with _client_lock:
        return session.client(service, region_name=region, config=config)


def partition_regions(session: boto3.Session) -> Tuple[str, List[str]]:
    """The partition this host is in, and its Glue regions from botocore's local data."""
    partition = "aws"
    if session.region_name:
        try:
            partition = session.get_partition_for_region(session.region_name)
        except Exception:  # noqa: BLE001 - an unknown region keeps the default
            pass
    return partition, sorted(session.get_available_regions("glue", partition))


# ------------------------------------------------------------------ discovery


@dataclass(frozen=True)
class Source:
    name: str
    database: str
    table: str
    ocsf_version: str
    columns: Tuple[str, ...]
    partitions: Tuple[str, ...]


@dataclass
class RegionState:
    state: str
    sources: Dict[str, Source] = field(default_factory=dict)
    detail: str = ""
    not_enabled: bool = False


def _ocsf_version(major: Optional[str], minor: Optional[str]) -> str:
    if major is None:
        return "unknown (custom source)"
    return _OCSF.get(major, f"unknown (source version {major}.{minor})")


def discover_region(session: boto3.Session, region: str) -> RegionState:
    try:
        glue = client(session, "glue", region, _DISCOVERY_CONFIG)
        sources: Dict[str, Source] = {}
        databases = [
            db["Name"]
            for page in glue.get_paginator("get_databases").paginate()
            for db in page.get("DatabaseList", [])
            if db["Name"].startswith(DB_PREFIX)
        ]
        for database in databases:
            for page in glue.get_paginator("get_tables").paginate(
                DatabaseName=database
            ):
                for table in page.get("TableList", []):
                    match = _TABLE.match(table["Name"])
                    if not match or match["source"] in sources:
                        continue
                    descriptor = table.get("StorageDescriptor") or {}
                    sources[match["source"]] = Source(
                        name=match["source"],
                        database=database,
                        table=table["Name"],
                        ocsf_version=_ocsf_version(match["major"], match["minor"]),
                        columns=tuple(
                            c["Name"].lower() for c in descriptor.get("Columns", [])
                        ),
                        partitions=tuple(
                            k["Name"].lower() for k in table.get("PartitionKeys", [])
                        ),
                    )
    except ClientError as exc:
        code = describe_exc(exc)
        if code in _DENIED or "LakeFormation" in code:
            return RegionState(REFUSED, detail=code)
        if code in _NOT_ENABLED:
            return RegionState(ABSENT, detail=code, not_enabled=True)
        return RegionState(UNREACHABLE, detail=code)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - a region that fails is reported, not dropped
        return RegionState(UNREACHABLE, detail=describe_exc(exc))
    if not sources:
        return RegionState(ABSENT)
    return RegionState(READABLE, sources)


@dataclass
class Discovery:
    partition: str
    regions: Dict[str, RegionState]
    error: str = ""

    def named(self, state: str) -> List[str]:
        return sorted(r for r, s in self.regions.items() if s.state == state)


def discover(session_factory: Callable[[], boto3.Session], budget: float) -> Discovery:
    """Every region of the partition at once. A region still out at ``budget`` is
    reported unreachable, never left out."""
    started = time.monotonic()
    try:
        session = session_factory()
        credentials = session.get_credentials()
        if credentials is None:
            raise NoCredentialsError()
        partition, regions = partition_regions(session)
    except Exception as exc:  # noqa: BLE001
        return Discovery("aws", {}, error=describe_exc(exc))
    try:
        # Once, before the fan-out: a role that cannot be assumed is not every
        # region refusing, and thirty threads should not each call AssumeRole.
        credentials.get_frozen_credentials()
    except Exception as exc:  # noqa: BLE001
        return Discovery(
            partition, {}, error=f"could not get credentials ({describe_exc(exc)})"
        )
    pool = ThreadPoolExecutor(max_workers=max(1, len(regions)))
    futures = {pool.submit(discover_region, session, r): r for r in regions}
    done, _ = wait(futures, timeout=max(0.5, budget - (time.monotonic() - started)))
    pool.shutdown(wait=False, cancel_futures=True)
    states = {
        region: (
            future.result()
            if future in done
            else RegionState(UNREACHABLE, detail=f"no answer within {budget:g} s")
        )
        for future, region in futures.items()
    }
    return Discovery(partition, states)


def summary(found: Discovery, allowed: Tuple[str, ...]) -> Tuple[bool, str]:
    """The Settings test line: readable, returning rows, refused, unreachable."""
    if found.error:
        return False, f"Could not read AWS ({found.error})"
    readable = found.named(READABLE)
    refused = found.named(REFUSED)
    unreachable = found.named(UNREACHABLE)
    answered = [s for s in found.regions.values() if s.state != UNREACHABLE]
    if (
        not readable
        and not refused
        and answered
        and all(s.not_enabled for s in answered)
    ):
        return False, "AWS rejected these credentials in every region"
    rows = [r for r in readable if r in allowed]

    def names(regions: List[str]) -> str:
        return ", ".join(regions) or "none"

    line = (
        f"Readable: {names(readable)}; returning rows: {names(rows)}; "
        f"refused: {names(refused)}"
    )
    if unreachable:
        line += f"; unreachable: {names(unreachable)}"
    if not readable:
        return False, f"No Security Lake source readable in {found.partition}. {line}"
    return True, line


# ------------------------------------------------------------------ the SQL check

_FORBIDDEN = {
    "insert", "update", "delete", "merge", "create", "drop", "alter", "unload",
    "grant", "revoke", "call", "execute", "prepare", "deallocate", "truncate",
    "vacuum", "optimize", "msck", "recursive",
}  # fmt: skip


@dataclass(frozen=True)
class Token:
    kind: str  # word, quoted, string, punct
    value: str
    start: int
    end: int


def tokenize(sql: str) -> List[Token]:
    """Split Athena SQL into tokens, comments dropped. Raises on anything unterminated."""
    tokens: List[Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            # Trino ends a line comment at \r as well as \n.
            ends = [j for j in (sql.find("\n", i), sql.find("\r", i)) if j >= 0]
            i = min(ends) + 1 if ends else n
        elif sql.startswith("/*", i):
            close = sql.find("*/", i + 2)
            if close < 0:
                raise LakeError("The SQL has an unterminated /* comment.")
            i = close + 2
        elif c in "'\"`":
            j = i + 1
            while True:
                j = sql.find(c, j)
                if j < 0:
                    raise LakeError("The SQL has an unterminated quoted string.")
                if sql.startswith(c * 2, j):  # doubled quote escapes itself
                    j += 2
                    continue
                break
            body = sql[i + 1 : j].replace(c * 2, c)
            tokens.append(Token("string" if c == "'" else "quoted", body, i, j + 1))
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            tokens.append(Token("word", sql[i:j], i, j))
            i = j
        else:
            tokens.append(Token("punct", c, i, i + 1))
            i += 1
    return tokens


def check_sql(sql: str) -> Tuple[List[Token], str]:
    """One read-only SELECT/WITH statement that names no Security Lake table.

    Returns the tokens and the SQL with any trailing semicolons removed.
    """
    tokens = tokenize(sql)
    while tokens and tokens[-1].value == ";" and tokens[-1].kind == "punct":
        tokens.pop()
    if not tokens:
        raise LakeError("The SQL is empty.")
    if any(t.kind == "punct" and t.value == ";" for t in tokens):
        raise LakeError("Only one statement per call: the SQL has more than one.")
    first = next(
        (t for t in tokens if not (t.kind == "punct" and t.value == "(")), None
    )
    if (
        first is None
        or first.kind != "word"
        or first.value.lower() not in ("select", "with")
    ):
        raise LakeError("Only a read-only SELECT or WITH statement can run.")
    for t in tokens:
        if t.kind == "word" and t.value.lower() in _FORBIDDEN:
            raise LakeError(
                f"{t.value.upper()} is not allowed: only a read-only SELECT or WITH "
                "statement can run."
            )
        if t.kind in ("word", "quoted") and t.value.lower().startswith(
            "amazon_security_lake"
        ):
            raise LakeError(
                "Name a source by its short name (e.g. cloud_trail_mgmt), not a "
                "Security Lake database or table: the server limits it to the "
                "time range for you."
            )
    return tokens, sql[: tokens[-1].end]


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _bounded(source: Source, start: datetime, end: datetime) -> str:
    """The source's table limited to [start, end) on its date partition and event time."""
    where: List[str] = []
    if "eventday" in source.partitions:
        last = end - timedelta(milliseconds=1)
        where.append(f"eventday BETWEEN '{start:%Y%m%d}' AND '{last:%Y%m%d}'")
    if "time" in source.columns:
        where.append(
            f"time >= {int(start.timestamp() * 1000)} AND time < {int(end.timestamp() * 1000)}"
        )
    elif "time_dt" in source.columns:
        where.append(
            f"time_dt >= from_iso8601_timestamp('{start.isoformat()}') "
            f"AND time_dt < from_iso8601_timestamp('{end.isoformat()}')"
        )
    else:
        raise LakeError(
            f"Source {source.name} has neither a time nor a time_dt column, so the "
            "time range cannot be applied."
        )
    return (
        f"{_ident(source.name)} AS (SELECT * FROM {_ident(source.database)}."
        f"{_ident(source.table)} WHERE {' AND '.join(where)})"
    )


def build_sql(
    sql: str, sources: Mapping[str, Source], start: datetime, end: datetime
) -> Tuple[str, List[Source]]:
    """The agent's SQL with each source short name it uses bound as a CTE."""
    tokens, body = check_sql(sql)
    named = sorted(
        {
            t.value.lower()
            for t in tokens
            if t.kind in ("word", "quoted") and t.value.lower() in sources
        }
    )
    if not named:
        raise LakeError(
            "The SQL names no source of this region. Use one of: "
            + ", ".join(sorted(sources))
            + "."
        )
    used = [sources[n] for n in named]
    ctes = ", ".join(_bounded(s, start, end) for s in used)
    first = next(t for t in tokens if t.kind == "word")
    if first.value.lower() == "with":
        return f"{body[: first.start]}WITH {ctes}, {body[first.end :].lstrip()}", used
    return f"WITH {ctes} {body}", used


def parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LakeError(
            f"{name} is required, as an ISO 8601 time (e.g. 2026-11-01T00:00:00Z)."
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise LakeError(f"{name} is not an ISO 8601 time.") from None
    return (
        parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    ).astimezone(timezone.utc)


# ------------------------------------------------------------------ querying


@dataclass
class QueryContext:
    region: str
    allowed: bool
    workgroup: str
    max_results: int
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    sources: List[Source] = field(default_factory=list)


_output_checked: Dict[Tuple[str, str], bool] = {}
_ranges: Dict[str, Tuple[str, str, List[str]]] = {}
_lock = threading.Lock()


def check_workgroup(athena, region: str, workgroup: str) -> None:
    """A missing workgroup or one with no output location is an error, never empty rows."""
    if _output_checked.get((region, workgroup)):
        return
    try:
        config = athena.get_work_group(WorkGroup=workgroup)["WorkGroup"].get(
            "Configuration", {}
        )
    except ClientError as exc:
        code = describe_exc(exc)
        if code == "InvalidRequestException":
            raise LakeError(
                f"Athena workgroup {workgroup!r} does not exist in {region}. Create it "
                "there with an output location, or set another workgroup."
            ) from None
        raise
    output = (config.get("ResultConfiguration") or {}).get("OutputLocation")
    managed = (config.get("ManagedQueryResultsConfiguration") or {}).get("Enabled")
    if not output and not managed:
        raise LakeError(
            f"Athena workgroup {workgroup!r} in {region} has no query result output "
            "location. Set one on the workgroup in that region."
        )
    with _lock:
        _output_checked[(region, workgroup)] = True


def _failure(ctx: QueryContext, query_id: str, status: Mapping[str, Any]) -> LakeError:
    reason = status.get("StateChangeReason") or ""
    error = status.get("AthenaError") or {}
    lowered = reason.lower()
    if (
        "lake formation" in lowered
        or "access denied" in lowered
        or "permission" in lowered
    ):
        return LakeError(
            f"Region {ctx.region} refused query {query_id}: Lake Formation or IAM did "
            "not grant read access to the source. This region is a visibility gap "
            "until the access is granted."
        )
    column = re.search(r"Column '([^']+)' cannot be resolved", reason)
    if column:
        versions = "; ".join(f"{s.name} is OCSF {s.ocsf_version}" for s in ctx.sources)
        return LakeError(
            f"Column {column.group(1)!r} does not exist in {ctx.region}'s source "
            f"({versions or 'version unknown'}). Ask again without it (query {query_id})."
        )
    kind = error.get("ErrorType") or error.get("ErrorCategory") or "unknown"
    if not ctx.allowed:
        # Athena's reason can quote row values (a failed cast, fail()): withheld.
        return LakeError(
            f"Athena failed query {query_id} in {ctx.region} (error {kind}). The "
            "reason is withheld: this region does not return rows to Vigil."
        )
    return LakeError(
        f"Athena failed query {query_id} in {ctx.region} (error {kind}): "
        f"{reason[:400] or 'no reason given'}"
    )


def _wait(
    athena, ctx: QueryContext, query_id: str, started: float
) -> Mapping[str, Any]:
    # Stop polling early enough that reading the results still fits the bound.
    deadline = started + SEARCH_DEADLINE_S - _FETCH_RESERVE_S
    pause = 0.5
    while True:
        execution = athena.get_query_execution(QueryExecutionId=query_id)[
            "QueryExecution"
        ]
        state = (execution.get("Status") or {}).get("State")
        if state == "SUCCEEDED":
            return execution
        if state in ("FAILED", "CANCELLED"):
            if state == "CANCELLED":
                raise LakeError(f"Query {query_id} in {ctx.region} was cancelled.")
            raise _failure(ctx, query_id, execution.get("Status") or {})
        if time.monotonic() + pause >= deadline:
            raise LakeError(
                f"Query {query_id} in {ctx.region} is still running; this call "
                f"stopped waiting after {time.monotonic() - started:.0f} s. It keeps "
                "running in Athena: "
                f"collect it later with region={ctx.region} and query_id={query_id}."
            )
        time.sleep(pause)
        pause = min(pause * 1.5, 3.0)


def answer(
    athena, ctx: QueryContext, query_id: str, execution: Mapping[str, Any]
) -> Dict[str, Any]:
    """The one place rows are fetched, and only for an allowed region.

    A region not allowed answers the pointer: row count from runtime statistics and
    bytes from the execution. ``GetQueryResults`` is never reached for it.
    """
    stats = execution.get("Statistics") or {}
    out: Dict[str, Any] = {
        "region": ctx.region,
        "query_id": query_id,
        "start": ctx.start.isoformat() if ctx.start else None,
        "end": ctx.end.isoformat() if ctx.end else None,
        "sources": [
            {"name": s.name, "ocsf_version": s.ocsf_version} for s in ctx.sources
        ],
        "bytes_scanned": stats.get("DataScannedInBytes"),
    }
    try:
        runtime = athena.get_query_runtime_statistics(QueryExecutionId=query_id)
        total: Optional[int] = (
            (runtime.get("QueryRuntimeStatistics") or {}).get("Rows") or {}
        ).get("OutputRows")
    except ClientError:
        total = None
    if not ctx.allowed:
        out.update(
            row_count=total,
            rows_withheld=True,
            note=(
                f"{ctx.region} is not in allowed_regions, so Vigil does not receive "
                "its result rows: this is a pointer to the query, not its results."
            ),
        )
        return out
    events: List[Dict[str, Any]] = []
    columns: List[str] = []
    token: Optional[str] = None
    header = True
    while len(events) < ctx.max_results:
        kwargs: Dict[str, Any] = {
            "QueryExecutionId": query_id,
            "MaxResults": min(
                1000, ctx.max_results - len(events) + (1 if header else 0)
            ),
        }
        if token:
            kwargs["NextToken"] = token
        page = athena.get_query_results(**kwargs)
        result_set = page.get("ResultSet") or {}
        if not columns:
            columns = [
                c["Name"]
                for c in (result_set.get("ResultSetMetadata") or {}).get(
                    "ColumnInfo", []
                )
            ]
        rows = result_set.get("Rows") or []
        if header and rows:  # a SELECT's first row is its column names
            rows = rows[1:]
        header = False
        for row in rows:
            values = [d.get("VarCharValue") for d in row.get("Data", [])]
            events.append(dict(zip(columns, values)))
        token = page.get("NextToken")
        if not token:
            break
    events = events[: ctx.max_results]
    out.update(
        row_count=total if total is not None else len(events),
        returned=len(events),
        rows_withheld=False,
        truncated=(total is not None and total > len(events)) or bool(token),
        ocsf_events=events,
    )
    return out


def run_query(
    session: boto3.Session,
    ctx: QueryContext,
    sql: str,
    region_sources: Mapping[str, Source],
    started: float,
) -> Dict[str, Any]:
    assert ctx.start and ctx.end
    final_sql, ctx.sources = build_sql(sql, region_sources, ctx.start, ctx.end)
    athena = client(session, "athena", ctx.region, _QUERY_CONFIG)
    check_workgroup(athena, ctx.region, ctx.workgroup)
    query_id = athena.start_query_execution(
        QueryString=final_sql, WorkGroup=ctx.workgroup
    )["QueryExecutionId"]
    with _lock:
        _ranges[query_id] = (
            ctx.start.isoformat(),
            ctx.end.isoformat(),
            [s.name for s in ctx.sources],
        )
    try:
        execution = _wait(athena, ctx, query_id, started)
        return answer(athena, ctx, query_id, execution)
    except LakeError:
        raise
    except Exception as exc:  # noqa: BLE001 - name the query so it can be collected
        raise LakeError(
            f"Query {query_id} in {ctx.region} could not be followed "
            f"({describe_exc(exc)}). Collect it with region={ctx.region} and "
            f"query_id={query_id}."
        ) from None


def collect(
    session: boto3.Session,
    ctx: QueryContext,
    query_id: str,
    region_sources: Mapping[str, Source],
    started: float,
) -> Dict[str, Any]:
    """An earlier query, under the same region rule as a fresh one."""
    athena = client(session, "athena", ctx.region, _QUERY_CONFIG)
    try:
        execution = athena.get_query_execution(QueryExecutionId=query_id)[
            "QueryExecution"
        ]
    except ClientError as exc:
        if describe_exc(exc) == "InvalidRequestException":
            raise LakeError(
                f"No Athena query {query_id} in {ctx.region}: a query is collected "
                "from the region it ran in."
            ) from None
        raise
    if (
        execution.get("WorkGroup") != ctx.workgroup
        or execution.get("StatementType") != "DML"
    ):
        raise LakeError(
            f"Query {query_id} in {ctx.region} is not a query this server ran "
            f"(workgroup {ctx.workgroup!r}); it cannot be collected here."
        )
    with _lock:
        known = _ranges.get(query_id)
    if known:
        ctx.start = datetime.fromisoformat(known[0])
        ctx.end = datetime.fromisoformat(known[1])
        ctx.sources = [region_sources[n] for n in known[2] if n in region_sources]
    execution = _wait(athena, ctx, query_id, started)
    return answer(athena, ctx, query_id, execution)
