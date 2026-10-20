from attention_budget.models import Item


def test_item_snippet_truncated_to_300():
    it = Item(id="xhs:1", platform="xhs", channel="follow", author="a", title="", snippet="字" * 500,
              url="u", published_at=None)
    assert len(it.snippet) == 300
