from pathlib import Path

import pytest

from triagerag.index.logs.drain import DEFAULT_STATE_PATH
from triagerag.shared.clean import CleanText
from triagerag.shared.extract import extract

pytestmark = pytest.mark.skipif(
    not Path(DEFAULT_STATE_PATH).exists(), reason="no trained Drain state"
)


def test_log_line_in_block_gets_template() -> None:
    text = CleanText(prose="", blocks=[
        "2013-02-14 17:29:58,128 INFO org.apache.hadoop.hdfs.DFSClient: Waiting for ack for: 42"
    ])
    ex = extract(text)
    assert len(ex.logs) == 1
    assert ex.logs[0].template_id is not None


def test_log_line_in_prose_found() -> None:
    text = CleanText(prose="The error:\n08/05/27 11:32:45 INFO mapred.JobClient: map 100% reduce 86%")
    ex = extract(text)
    assert len(ex.logs) == 1
    assert ex.logs[0].source == "prose"
    assert ex.logs[0].message == "map 100% reduce 86%"


def test_exception_line_found() -> None:
    text = CleanText(prose="java.io.IOException: Could not get block locations. Aborting...\nat org.apache.hadoop.dfs.X")
    ex = extract(text)
    assert ex.exception_classes == {"java.io.IOException"}


def test_plain_prose_not_a_log() -> None:
    ex = extract(CleanText(prose="The INFO level is too noisy: we should lower it."))
    assert ex.logs == [] and ex.exceptions == []