"""The Logs search language: what people type, read into SQL over log_events."""

from sqlalchemy.dialects import postgresql

from core.log_events.query import Term, parse, search_text, where


def sql(q: str) -> str:
    return str(
        where(q).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_words_phrases_keys_negation_and_scores():
    assert parse(
        'powershell "failed logon" host:FINANC -source:entra loglm>0.7 user:"a b"'
    ) == [
        Term(None, "powershell"),
        Term(None, "failed logon"),
        Term("host", "FINANC"),
        Term("source", "entra", negate=True),
        Term("loglm", "0.7", op=">"),
        Term("user", "a b"),
    ]


def test_unknown_keys_and_urls_stay_free_text():
    assert parse("https://x.example/a team:blue") == [
        Term(None, "https://x.example/a"),
        Term(None, "team:blue"),
    ]


def test_an_unclosed_quote_reads_as_words():
    assert parse('unterminated "quote') == [
        Term(None, "unterminated"),
        Term(None, "quote"),
    ]


def test_empty_search_matches_everything():
    assert where("") is None
    assert where("   ") is None


def test_free_words_match_the_lowercased_search_text():
    s = sql("PowerShell")
    assert "log_events.search_text LIKE" in s and "'powershell'" in s


def test_comma_means_either_and_minus_leaves_out():
    s = sql("sev:high,critical -action:deny")
    assert "log_events.severity = 'high' OR log_events.severity = 'critical'" in s
    assert "NOT coalesce(lower(log_events.action) = 'deny', false)" in s


def test_minus_keeps_events_without_the_field():
    # NOT (incident = 'INC-7') is NULL for an everyday event with no incident,
    # which would drop it too
    assert (
        sql("-incident:INC-7")
        == "NOT coalesce(lower(log_events.incident) = 'inc-7', false)"
    )
    assert sql("-user:j.doe").startswith("NOT coalesce(")


def test_backslashes_are_kept_and_match_the_json_raw_record():
    assert parse(r"EXAMPLE\j.doe C:\Windows\Temp") == [
        Term(None, r"EXAMPLE\j.doe"),
        Term(None, r"C:\Windows\Temp"),
    ]
    s = sql(r"example\j.doe")
    # the summary has one backslash, the raw record's JSON has two; LIKE must
    # not read either as an escape
    assert r"'example\j.doe'" in s and r"'example\\j.doe'" in s
    assert "ESCAPE '/'" in s


def test_source_words_people_use():
    assert "log_events.source IN ('paloalto')" in sql("source:firewall")
    assert "log_events.source IN ('crowdstrike')" in sql("source:falcon")


def test_partial_ip_is_a_prefix_and_full_ip_is_exact():
    assert "log_events.src_ip = '10.0.10.20'" in sql("ip:10.0.10.20")
    assert "log_events.src_ip LIKE '10.0.10.' ||" in sql("ip:10.0.10.")


def test_scores_compare():
    assert "log_events.loglm > 0.9" in sql("loglm>0.9")
    assert "log_events.loglm <= 0.2" in sql("loglm<=0.2")


def test_search_text_covers_the_readable_fields_and_the_raw_record():
    text = search_text(
        {
            "summary": "Failed Logon",
            "src_host": "CR-10",
            "user_name": "f.ortiz",
            "tags": ["beacon"],
            "raw": {"Image": "PowerShell.exe"},
        }
    )
    for word in ("failed logon", "cr-10", "f.ortiz", "beacon", "powershell.exe"):
        assert word in text


def test_short_aliases_ports_ip_prefixes_and_wildcards():
    assert sql("source:admin") == "log_events.source IN ('admin')"  # not winsec's "ad"
    assert sql("source:firewalls") == "log_events.source IN ('paloalto')"
    assert sql("port:https") == "false" and sql("port:99999") == "false"
    assert "'10.0.10.'" in sql("ip:10.0.10")  # not 10.0.100.x
    assert (
        sql("ip:2001:db8::1")
        == "log_events.src_ip = '2001:db8::1' OR log_events.dst_ip = '2001:db8::1'"
    )
    assert "svc/_backup" in sql("user:svc_backup")
    assert sql("sev:,") == "false"
