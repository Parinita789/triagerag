import pytest

from triagerag.query.logs import LogQueryError, build_logql, validate

KNOWN = {"component": ["dfs.FSNamesystem"], "level": ["INFO", "WARN"]}
S = "2008-11-10T10:00:00Z"


def ok(**kw):
    args = dict(start=S, end="2008-11-10T11:00:00Z", block_id=None, contains=None,
                component=None, level=None, known=KNOWN)
    args.update(kw)
    return validate(**args)


def test_block_filter_does_not_match_longer_ids():
    assert '|~ "blk_-123([^0-9]|$)"' in build_logql("blk_-123", None, None, None)


def test_selector_always_present():
    assert build_logql(None, None, None, None) == '{level=~".+"}'


def test_range_over_6h_rejected():
    with pytest.raises(LogQueryError):
        ok(end="2008-11-10T17:00:00Z")


def test_end_before_start_rejected():
    with pytest.raises(LogQueryError):
        ok(end="2008-11-10T09:00:00Z")


def test_quote_injection_rejected():
    with pytest.raises(LogQueryError):
        ok(contains='x" or "')


def test_unknown_component_rejected():
    with pytest.raises(LogQueryError):
        ok(component="dfs.Nope")


def test_bad_block_id_rejected():
    with pytest.raises(LogQueryError):
        ok(block_id="blk_abc")


def test_valid_query_passes():
    s, e = ok(block_id="blk_-123", level="WARN")
    assert e - s == 3600 * 1_000_000_000