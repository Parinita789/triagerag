from triagerag.shared.redact import find_spans, redact
import pytest


def test_redacted_text_has_no_leaks() -> None:
    text = "email alice@corp.com, key AKIAIOSFODNN7EXAMPLE, token=HAAEaGRmcwRoZGZzAIoBRp2bNByKAUbBp7gc"
    masked, _ = redact(text)
    assert find_spans(masked) == []


def test_same_value_same_pseudonym() -> None:
    a, _ = redact("contact alice@corp.com")
    b, _ = redact("reply to alice@corp.com please")
    tag = a.split()[-1]
    assert tag in b


def test_example_domain_allowed() -> None:
    _, counts = redact("principal hdfs/nn1.example.com@EXAMPLE.COM")
    assert counts["EMAIL"] == 0


@pytest.mark.parametrize("text", [
    "blockpool BP-1204772930-172.16.165.209-1478761131832 block",
    "storageID=DS-443871816-XX.XX.XX.XX-50276-1336829714197, infoPort=50275",
    "PacketResponder 0 for Block blk_-2322514873363546651",
    "!image-2019-10-21-18-05-19-160.png|width=31!",
    "Token<?> token = fs.getDelegationToken(renewer);",
    "WARN Token: No TokenRenewer defined for token kind",
    "jetty.ssl.password : jetty.ssl.keypassword :",
    "reset your Apache LDAP password: <deleted-url>",
])
def test_real_false_positives_not_redacted(text: str) -> None:
    assert find_spans(text) == []


@pytest.mark.parametrize("text", [
    "token=HAAEaGRmcwRoZGZzAIoBRp2bNByKAUbBp7gcbBQUD6vWmRYJRv03XZj7",
    "accessToken=eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3MjY2NjQxNjZ9.DwDBnJ6I8vCFd14A-wsq2oLU5a0rcPoUvq49Z4aWg2A",
])
def test_real_secrets_redacted(text: str) -> None:
    assert find_spans(text) != []