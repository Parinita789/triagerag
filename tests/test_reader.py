import pytest
from loginsights.ingest.reader import parse_line, BLOCK_RE

def test_standard_line():
    raw = ("081109 203518 143 INFO dfs.DataNode$DataXceiver: "
           "Receiving block blk_-1608999687919862906 "
           "src: /10.250.19.102:54106 dest: /10.250.19.102:50010")
    p = parse_line(raw)
    assert p is not None
    assert p.ts.year == 2008
    assert p.pid == 143
    assert p.level == "INFO"
    assert p.component == "dfs.DataNode$DataXceiver"
    assert p.block_ids == ["blk_-1608999687919862906"]
    assert p.message.startswith("Receiving block")

def test_negative_and_positive_block_ids():
    assert BLOCK_RE.findall("blk_123 and blk_-456") == ["blk_123", "blk_-456"]

@pytest.mark.parametrize("raw", ["", "garbage", "081109 203518"])
def test_malformed_returns_none(raw: str):
    assert parse_line(raw) is None