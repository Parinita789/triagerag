from pathlib import Path

import pytest

from triagerag.index.logs.drain import DEFAULT_STATE_PATH
from triagerag.shared.normalize import normalize

pytestmark = pytest.mark.skipif(
    not Path(DEFAULT_STATE_PATH).exists(),
    reason="no trained Drain state; run scripts/train_drain.py first",
)


def test_terminating_and_interrupted_are_different_templates() -> None:
    terminating = normalize("PacketResponder 1 for block blk_38865049064139660 terminating")
    interrupted = normalize("PacketResponder 0 for block blk_4241467193520768333 Interrupted.")

    assert terminating.template_id is not None
    assert interrupted.template_id is not None
    assert terminating.template_id != interrupted.template_id


def test_different_ips_and_blocks_same_template() -> None:
    a = normalize("10.251.43.21:50010:Transmitted block blk_-1608999687919862906 to /10.250.7.230:50010")
    b = normalize("10.251.65.237:50010:Transmitted block blk_7503483334202473044 to /10.251.30.179:50010")

    assert a.template_id is not None
    assert a.template_id == b.template_id


def test_exception_class_extracted() -> None:
    n = normalize(
        "writeBlock blk_-4214617012290386543 received exception "
        "java.net.SocketTimeoutException: 480000 millis timeout while waiting for channel "
        "to be ready for write. ch : java.nio.channels.SocketChannel"
        "[connected local=/10.251.203.80:50010 remote=/10.251.65.237:52396]"
    )

    assert n.template_id is not None
    assert n.exception == "java.net.SocketTimeoutException"


def test_no_exception_when_line_has_none() -> None:
    n = normalize("Verification succeeded for blk_-1608999687919862906")

    assert n.template_id is not None
    assert n.exception is None


def test_unknown_line_returns_none() -> None:
    n = normalize("hello world")

    assert n.template_id is None
    assert n.exception is None

def test_interrupted_io_exception_extracted() -> None:
    n = normalize(
        "PacketResponder blk_4241467193520768333 1 Exception java.io.InterruptedIOException: "
        "Interruped while waiting for IO on channel java.nio.channels.SocketChannel"
        "[connected local=/10.251.123.33:39066 remote=/10.250.7.32:50010]. 59942 millis timeout left."
    )

    assert n.template_id is not None
    assert n.exception == "java.io.InterruptedIOException"