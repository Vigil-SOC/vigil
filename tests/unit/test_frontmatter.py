"""The shared frontmatter reader behind WORKFLOW.md, SKILL.md and INTENT.md."""

import pytest

from core.frontmatter import FrontmatterError, split_frontmatter

pytestmark = pytest.mark.unit


def test_mapping_and_body_offset():
    text = "---\nname: x\nlist: [a, b]\n---\n\n# Body\n"
    front, offset = split_frontmatter(text)
    assert front == {"name": "x", "list": ["a", "b"]}
    assert text[offset:].strip() == "# Body"


def test_no_block_is_none_and_whole_body():
    assert split_frontmatter("# just markdown\n") == (None, 0)


def test_empty_block_is_empty_mapping():
    front, offset = split_frontmatter("---\n# nothing\n---\nbody")
    assert front == {} and "body" == "---\n# nothing\n---\nbody"[offset:]


def test_body_may_start_on_the_closing_line():
    # The skill importer accepted this before the hoist; kept.
    front, offset = split_frontmatter("---\nname: x\n---body")
    assert front == {"name": "x"} and "---\nname: x\n---body"[offset:] == "body"


@pytest.mark.parametrize("bad", ["---\n: [oops\n---\nbody", "---\n- a\n- b\n---\nbody"])
def test_invalid_or_non_mapping_raises_with_body_offset(bad):
    with pytest.raises(FrontmatterError) as excinfo:
        split_frontmatter(bad)
    assert bad[excinfo.value.body_offset :] == "body"
