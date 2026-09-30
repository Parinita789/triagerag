from triagerag.shared.clean import clean_text, clean_ticket, split_blocks, strip_markup


def test_markup_stripped() -> None:
    assert strip_markup("use {{rm}} and *careful*") == "use rm and careful"
    assert strip_markup("h2. Scope\nsee [the doc|http://x.org]") == "Scope\nsee the doc"
    assert strip_markup("{{{}Options.Rename.TO_TRASH{}}}") == "Options.Rename.TO_TRASH"


def test_list_bullets_survive() -> None:
    assert strip_markup("* first\n* second") == "* first\n* second"


def test_blocks_extracted_and_untouched() -> None:
    raw = "before\n{code:java}\nint *x = y{}; // *not bold*\n{code}\nafter"
    prose, blocks = split_blocks(raw)
    assert "int *x" not in prose
    assert blocks == ["int *x = y{}; // *not bold*"]


def test_secrets_redacted_inside_blocks() -> None:
    raw = "{noformat}\ncurl ...&token=HAAEaGRmcwRoZGZzAIoBRp2bNByKAUbBp7gcbBQU\n{noformat}"
    text, counts = clean_text(raw)
    assert "HAAEaGRm" not in text.blocks[0]
    assert counts["CREDENTIAL"] == 1


def test_bot_comments_dropped() -> None:
    issue = {
        "key": "HDFS-1",
        "fields": {
            "summary": "s",
            "description": "d",
            "comment": {"comments": [
                {"author": {"name": "hadoopqa"}, "created": "t", "body": "-1 overall"},
                {"author": {"name": "szetszwo"}, "created": "t", "body": "Looks like a lease issue."},
            ]},
        },
    }
    t = clean_ticket(issue)
    assert t.bot_comments_dropped == 1
    assert [c.author for c in t.comments] == ["szetszwo"]


def test_block_indentation_kept() -> None:
    _, blocks = split_blocks("{code}\n  <property>\n    <name>x</name>\n{code}")
    assert blocks == ["  <property>\n    <name>x</name>"]    


def test_unclosed_block_becomes_block() -> None:
    prose, blocks = split_blocks("Cannot reproduce.\n{code}\n$ hadoop fs -ls\nFound 1 items")
    assert prose.strip() == "Cannot reproduce."
    assert blocks == ["$ hadoop fs -ls\nFound 1 items"]