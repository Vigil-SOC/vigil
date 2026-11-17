#!/usr/bin/env python3
# Gates that the two halves that mint Entity Keys agree: the hunt harness (TypeScript,
# services/agent/plugins/hunt) and memory (Python, core/memory + core/storage/defang.py),
# plus the console's list of entity types. Each half carries its own copy of the rules,
# so a change to one alone silently turns "history for this entity" into "no history".
#
# It RUNS both halves on the same inputs and compares their outputs to each other. It
# holds inputs only, never expected keys, so a rule changed in both halves passes without
# editing this file. Only moving an entry point (ENTRY_POINTS in the driver, and below)
# or declaring an intended difference (REFUSED_BY_DESIGN) needs an edit here.
#
# Limit (V4): lowercasing of non-ASCII text follows each runtime's Unicode tables and is
# not compared; a runtime upgrade can change a result. A failure prints both versions so
# a runtime difference is recognisable from a rule difference. Inputs stay ASCII-folding.
#
# Needs `npm ci` in services/agent (for tsx). Run from anywhere.

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "services" / "agent"
DRIVER = ROOT / "scripts" / "entity_key_agreement_driver.mts"
sys.path.insert(0, str(ROOT))

# Python's entry points, by module. The harness's and the console's are in the driver.
ENTRY_POINTS = {
    "core.memory.entity_keys": ["entity_key", "normalise_key", "text_entity_keys"],
    "core.memory.recall_contract": ["ENTITY_KEY_TYPES", "KEY_CASE_SENSITIVE_TYPES"],
    "core.memory.tlds": ["TLDS"],
    "core.storage.defang": ["defang"],
}

# Declared differences. The harness refuses these subjects by design -- an unknown type,
# or a malformed value -- and memory's `entity_key` keys them, since it checks neither.
# Any other subject the harness refuses and Python keys fails. A declaration that stops
# holding (the halves now agree) fails too, so it cannot go stale unseen.
UNKNOWN_TYPE = "the harness refuses an unknown type (the closed set stops a typo minting a kind); memory keys it"
MALFORMED = "the harness refuses a malformed value (wellFormed in entities.ts); entity_key does not check"
REFUSED_BY_DESIGN = {
    "mutex:Global\\Foo": UNKNOWN_TYPE,
    "ipp:1.2.3.4": UNKNOWN_TYPE,
    "ip:203.0.113.999": MALFORMED,
    "ip:not-an-ip": MALFORMED,
    "ip:-": MALFORMED,
    "host:null": MALFORMED,
    "host:-": MALFORMED,
    "user:null": MALFORMED,
    "domain:com": MALFORMED,
    "email:com": MALFORMED,
    "domain:nodot": MALFORMED,
    "domain:example.invalid": MALFORMED,
    "domain:backup.zip": MALFORMED,
    "email:nobody": MALFORMED,
    "email:bob@example.invalid": MALFORMED,
    "hash:abc123": MALFORMED,
}

SHOWN_PER_RULE = 3

# Every character either half trims at a value's edge today, plus ordinary spaces.
EDGE = ["\ufeff", "\x1c", "\x1d", "\x1e", "\x1f", "\x85", "\xa0", "\u3000", "\u2003", " ", "\t"]
# JS `\s` plus the ones it leaves out, which a URL or ARN may therefore contain.
BOUNDARY = ["\xa0", "\u2003", "\ufeff", "\u2028", "\x85", "\x1c", "\u180e", " ", "\n"]

# (rule, input). Rule names what a mismatch on this input points at.
SUBJECTS: list[tuple[str, str]] = []
TEXTS: list[tuple[str, str]] = []
DEFANGS: list[tuple[str, str]] = []


def subject(rule: str, *inputs: str) -> None:
    SUBJECTS.extend((rule, i) for i in inputs)


def text(rule: str, *inputs: str) -> None:
    TEXTS.extend((rule, i) for i in inputs)


subject(
    "case fold: partial fold of a process path",
    "process:/usr/Bin/Python3", "process:C:\\Windows\\System32\\CMD.EXE",
    "process:Relative/Path.EXE", "process:/", "process:Notepad.EXE",
)
subject(
    "case fold: partial fold of a url (scheme and authority only)",
    "url:HTTPS://Example.COM/Path/CaseKept?Q=Val#Frag", "url:HTTP://EXAMPLE.com",
    "url:Example.COM/Path", "url:https://EXAMPLE.com?Q=1", "url:https://EXAMPLE.com#Frag",
    "url:HTTPS://User@Example.COM:8080/Path", "url:Example.COM/a?b=HTTP://X/Y",
)
subject(
    "case fold: case-sensitive types and every other type's fold",
    "arn:arn:aws:iam::123456789012:role/Admin", "ARN:arn:AWS:s3:::Bucket/Key",
    "aws_key:AKIAIOSFODNN7EXAMPLE", "AWS_KEY:AKIAIOSFODNN7EXAMPLE",
    "user:Alice@CORP", "host:WEB-01.Corp.Local", "email:Bob@Example.COM",
    "hash:D41D8CD98F00B204E9800998ECF8427E", "ip:2001:DB8::1", "domain:Evil.EXAMPLE.com",
    "IP:10.0.0.1", "Domain : x.com",
)
subject(
    "defang: every token",
    "domain:evil[.]example[.]com", "domain:evil(.)example(.)com", "domain:evil{.}example{.}com",
    "url:hxxp://x.com", "url:hXXps://x.com", "url:HXXP://x.com", "url:http[:]//x.com",
    "email:bob[at]example.com", "email:bob[AT]example.com", "url:hxxp[:]//x[[.]]com",
    "url:hxxp://x[.]com/a[.]B",
)
subject(
    "defang: hxxp word boundary (V1)",
    "url:éhxxp://www.rfc-editor.org/rfc/rfc3986", "url:1hxxp://x.com", "url:xhxxp://x.com",
    "url:٣hxxp://x.com", "url:_hxxp://x.com", "url: hxxp://x.com",
)
for c in EDGE:
    subject(
        "edge trim: a subject's value",
        f"ip:{c}203.0.113.7", f"ip:203.0.113.7{c}", f"domain:{c}Example.com{c}",
        f"url:https://x.com/a{c}", f"arn:arn:aws:iam::1:role/R{c}",
    )
    subject("edge trim: a subject's type and raw edge", f"{c}ip:1.2.3.4", f"ip{c}:1.2.3.4", f"{c}ip:{c}1.2.3.4")
subject("edge trim: value that trims to nothing", "ip:", "ip: ", "ip:\ufeff", "ip:\x1c", "host:\x85", ":x", "nocolon", "")
subject("subject parsing: split on the first colon only", "url:https://x.com:8080/a:b", "ip:::1", "ip:fe80::1%eth0")
subject("subject parsing: refusals by design", *REFUSED_BY_DESIGN)

text(
    "extraction pattern: arn",
    'role arn:aws:iam::123456789012:role/Admin, and', "ARN:AWS:S3:::Bucket/Key", 'x "arn:aws:s3:::b/k" y',
    "éarn:aws:s3:::b/k", "arn:aws:iam::1:role/R'quoted", "arn:aws::x", "arn:aws:s3:::b/k.",
)
text(
    "extraction pattern: aws_key (case-sensitive flag)",
    "key AKIAIOSFODNN7EXAMPLE here", "ASIAIOSFODNN7EXAMPLE AIDAIOSFODNN7EXAMPLE AROAIOSFODNN7EXAMPLE",
    "akiaiosfodnn7example", "AKIAIOSFODNN7EXAMPL", "AKIAIOSFODNN7EXAMPLEX", "éAKIAIOSFODNN7EXAMPLE",
)
text(
    "extraction pattern: url",
    'see "https://x.com/a" ok', "<http://Example.COM/Path?Q=1#F>", "HTTPS://X.COM/Path",
    "https://x.com/a'b", "éhttps://x.com/a", "ftp://x.com/a", "https://",
)
text(
    "extraction pattern: email",
    "mail Bob@Example.COM now", "a.b+c_d%e@sub.example.co.uk", "bob@example", "bob@example.invalid",
    "bob@example.abcdefghijklmnopqrstuvwx", "bob@example.abcdefghijklmnopqrstuvwxy", "é@example.com",
)
text(
    "extraction pattern: hash",
    "d41d8cd98f00b204e9800998ecf8427e", "D41D8CD98F00B204E9800998ECF8427E",
    "da39a3ee5e6b4b0d3255bfef95601890afd80709", "a" * 64, "a" * 63, "a" * 31, "a" * 41,
)
text(
    "extraction pattern: ip v4 and v6",
    "from 203.0.113.7 to 10.0.0.1", "999.1.1.1", "1.2.3", "1.2.3.4.5", "203.0.113.999",
    "::1", "fe80::1", "2001:DB8:0:0:0:0:0:1", "1:2:3", ":::", "2001:db8::1::2", "12345::1", "a:b",
    "١.٢.٣.٤", "٣1.2.3.4",
)
text(
    "extraction pattern: domain and the TLD list",
    "evil.example.com", "Evil.EXAMPLE.com", "foo.invalid", "backup.zip run.js a.py b.sh c.pl d.cab e.prof f.mov",
    "xn--mgbaam7a8h and a.xn--mgbaam7a8h", "a.abcdefghijklmnopqrstuvwx", "a.abcdefghijklmnopqrstuvwxy", "a.b.travelersinsurance", "bob@b.americanexpress",
    "-a.com a-.com a--b.com", "example.com.", "a..com", "é.com", "ex.é.com",
)
text(
    "extraction order and dedupe",
    "https://evil.com/x and bob@evil.com and evil.com 1.1.1.1 evil.com", "EVIL.com evil.COM",
)
text(
    "defang: every token in free text",
    "evil[.]example[.]com", "evil(.)example(.)com", "evil{.}example{.}com", "hxxp://x.com", "hxxps://x[.]com/p",
    "bob[at]example.com", "http[:]//x.com", "2001[:]db8[:][:]1",
)
text("defang: hxxp word boundary (V1)", "éhxxp://www.rfc-editor.org/rfc/rfc3986", "ahxxp://x.com 1hxxp://y.com")
for c in BOUNDARY:
    text(
        "extraction pattern: whitespace class around a url or ARN",
        f"https://x.com/a{c}tail", f"arn:aws:s3:::b/k{c}tail", f"https://x.com/a{c}", f"a{c}https://x.com/b",
    )
text(
    "second defang and trim of an extracted value (V3), and its order after the trailing-punctuation trim",
    "https://www.rfc-editor[[.]]org/rfc/RFC3986",
    "https://www.rfc-editor.org/rfc/rfc3986\x1c", "https://www.rfc-editor.org/rfc/rfc3986\x85",
    "https://www.rfc-editor.org/rfc/rfc3986.\x1c", "https://www.rfc-editor.org/rfc/rfc3986[[.]]",
    "arn:aws:s3:::Bucket/Key\x1c", "arn:aws:s3:::Bucket/Key.\x85", "arn:aws:s3:::Bucket[[.]]x/Key",
    "https://www.rfc-editor[[:]]org/RFC", "https://x.com/a\ufeff", "https://x.com/a\x1d\x1e\x1f",
    "https://Example[[.]]COM/Path", "https://x.com/a[[at]]b", "https://x.com/hxxp",
)
text(
    "trailing-punctuation trim",
    "(see https://x.com/a).", "https://x.com/a.,;)", "https://x.com/a);", "(1.2.3.4).", "host evil.example.com, then",
    "arn:aws:s3:::b/k);", "mail bob@example.com;", "https://x.com/a(b)", "https://x.com/a.b.",
)
text(
    "version-word rule",
    "v1.2.3.4", "version: 10.0.0.1", "release=1.2.3.4", "Build 1.2.3.4", "VER.1.2.3.4", "ver \xa01.2.3.4",
    "xv1.2.3.4", "seen at 1.2.3.4", "version     1.2.3.4", "version       1.2.3.4", "éversion 1.2.3.4",
    "v 1.2.3.4 and 5.6.7.8", "version 1.2.3.4.5", "version\x1c1.2.3.4", "version\ufeff1.2.3.4",
)
text("well-formed checks", "bob@example.zip", "x.zip", "10.0.0.1/24", "[2001:db8::1]", "evil.example.com:8080")

# Strings for `defang` itself.
DEFANGS.extend(
    ("defang: every token", s)
    for s in ("a[.]b(.)c{.}d", "hxxp hXXp HXXP hxXp", "[:] [at] [AT] [At]", "[[.]] [[:]] [[at]]", "hxxxxp hxxxp hxp", "éhxxp 1hxxp _hxxp -hxxp")
)


def run_harness(payload: dict) -> dict:
    exe = AGENT / "node_modules" / ".bin" / "tsx"
    if not exe.exists():
        sys.exit(f"entity-key agreement: {exe} is missing; run `npm ci` in services/agent first")
    done = subprocess.run(
        [str(exe), str(DRIVER)], cwd=AGENT, input=json.dumps(payload), capture_output=True, text=True,
    )
    try:
        return json.loads(done.stdout)
    except ValueError:
        sys.exit(f"entity-key agreement: the harness driver failed to run\n{done.stdout}\n{done.stderr}")


def load_python() -> tuple[dict, list[str]]:
    found: dict = {}
    missing: list[str] = []
    for module, names in ENTRY_POINTS.items():
        try:
            mod = importlib.import_module(module)
        except Exception as error:  # a moved or broken module is reported by name
            missing.append(f"{', '.join(names)} ({module} would not import: {error})")
            continue
        for name in names:
            if hasattr(mod, name):
                found[name] = getattr(mod, name)
            else:
                missing.append(f"{name} ({module})")
    return found, missing


def show(value: object) -> str:
    return repr(value)


def main() -> int:
    py, py_missing = load_python()
    payload = {
        "subjects": [i for _, i in SUBJECTS],
        "texts": [i for _, i in TEXTS],
        "defangs": [i for _, i in DEFANGS],
    }
    ts = run_harness(payload)
    versions = f"node {ts['node']}, python {sys.version.split()[0]}"
    if ts["missing"] or py_missing:
        print("entity-key agreement: an entry point the check reaches a half through has moved\n")
        for name in ts["missing"]:
            print(f"  harness / console: {name}")
        for name in py_missing:
            print(f"  python: {name}")
        print("\nUpdate the entry point in scripts/check_entity_key_agreement.py (Python) or")
        print("scripts/entity_key_agreement_driver.mts (harness, console) to where it lives now.")
        return 1

    failures: list[tuple[str, str]] = []

    def disagree(rule: str, what: str, harness: object, python: object) -> None:
        failures.append((rule, f"  [{rule}] {what}\n      harness: {show(harness)}\n      python : {show(python)}"))

    # Lists, by value.
    lists = ts["lists"]
    py_lists = {
        "entity_types": set(py["ENTITY_KEY_TYPES"]),
        "case_sensitive_types": set(py["KEY_CASE_SENSITIVE_TYPES"]),
        "tlds": set(py["TLDS"]),
    }
    for name, label in (("entity_types", "entity types"), ("case_sensitive_types", "case-sensitive types"), ("tlds", "TLD set after exclusions")):
        harness_set, python_set = set(lists[name]), py_lists[name]
        if harness_set != python_set:
            disagree(f"list: {label}", "only in one half", f"only in harness: {sorted(harness_set - python_set)[:20]}", f"only in python: {sorted(python_set - harness_set)[:20]}")
    if set(lists["console_entity_types"]) != py_lists["entity_types"]:
        console_set, python_set = set(lists["console_entity_types"]), py_lists["entity_types"]
        disagree("list: entity types (console)", "console vs python", f"console only: {sorted(console_set - python_set)}", f"python only: {sorted(python_set - console_set)}")

    # defang itself.
    for (rule, raw), harness in zip(DEFANGS, ts["defangs"]):
        python = py["defang"](raw)
        if harness != python:
            disagree(rule, f"input {show(raw)}", harness, python)

    # Subjects: the harness's parseKey against memory's key minting.
    declared: list[str] = []
    for (rule, raw), harness in zip(SUBJECTS, ts["subjects"]):
        python = py["normalise_key"](raw) or None
        if raw in REFUSED_BY_DESIGN:
            if harness is None and python is not None:
                declared.append(f"  {show(raw)}: {REFUSED_BY_DESIGN[raw]} (python keys {show(python)})")
            else:
                disagree("declared difference no longer holds", f"input {show(raw)} ({REFUSED_BY_DESIGN[raw]})", harness, python)
        elif harness != python:
            disagree(rule, f"subject {show(raw)}", harness, python)

    # Free text: each half's own keys, deduped in order.
    for (rule, raw), harness in zip(TEXTS, ts["texts"]):
        python = py["text_entity_keys"](raw)
        if list(dict.fromkeys(harness)) != python:
            disagree(rule, f"text {show(raw)}", list(dict.fromkeys(harness)), python)

    checked = f"{len(SUBJECTS)} subjects, {len(TEXTS)} texts, {len(DEFANGS)} defang strings, 4 lists"
    if declared:
        print("entity-key agreement: declared differences (reported, not failures):")
        print("\n".join(declared))
    if failures:
        print(f"\nentity-key agreement: FAIL, {len(failures)} disagreement(s) between the harness and memory ({versions})\n")
        shown: dict[str, int] = {}
        for rule, report in failures:
            shown[rule] = shown.get(rule, 0) + 1
            if shown[rule] <= SHOWN_PER_RULE:
                print(report)
        for rule, count in shown.items():
            if count > SHOWN_PER_RULE:
                print(f"  [{rule}] ... and {count - SHOWN_PER_RULE} more inputs")
        print(
            "\nThe harness (services/agent/plugins/hunt/entities.ts, types.ts, tlds.ts, contracts/memory.ts),"
            "\nmemory (core/memory/entity_keys.py, recall_contract.py, tlds.py, core/storage/defang.py) and the"
            "\nconsole (clients/web/src/screens/workflows/entityTypes.ts) must build the same Entity Key."
            "\nMake the same change in each half. If the difference is intended, declare it in"
            "\nREFUSED_BY_DESIGN here, with its reason. A difference that shows on non-ASCII text and"
            "\nnames both runtimes' versions above may be Unicode tables, not a rule (V4)."
        )
        return 1

    print(f"entity-key agreement: ok ({checked}; {versions})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
