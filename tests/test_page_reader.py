from browser.page_reader import (
    format_elements,
    format_page_text,
    lead_text,
    parse_snapshot,
)

SNAPSHOT = """
- main:
  - heading "Example Domain" [level=1]
  - paragraph: This domain is for documentation examples.
  - textbox "Search"
  - button "Submit"
  - link "Learn more":
    - /url: https://iana.org/help/example-domains
  - textbox "Search"
  - button "Delete" [disabled]
  - option "Choose one"
"""


def test_parse_assigns_refs_and_deduplicates() -> None:
    result = parse_snapshot(SNAPSHOT, page_url="https://example.com", limit=60)

    assert [ref.ref for ref in result.refs] == ["e1", "e2", "e3", "e4", "e5", "e6"]
    assert result.refs[0].role == "textbox"
    assert result.refs[0].nth == 0
    assert result.refs[3].name == "Search"
    assert result.refs[3].nth == 1
    assert result.refs[4].disabled is True
    assert result.refs[2].href == "https://iana.org/help/example-domains"
    assert not result.truncated


def test_parse_respects_limit() -> None:
    result = parse_snapshot(SNAPSHOT, page_url="https://example.com", limit=2)
    assert len(result.refs) == 2
    assert result.truncated is True


def test_format_elements_contains_refs_and_links() -> None:
    result = parse_snapshot(SNAPSHOT, page_url="https://example.com")
    rendered = format_elements(result.refs)
    assert '[e1] textbox "Search"' in rendered
    assert "iana.org" in rendered


def test_empty_page_text_is_explicit() -> None:
    rendered = format_page_text(title="Blank", url="https://example.com", text="   ")
    assert "(The page has no readable text.)" in rendered


def test_page_text_chunks_and_marks_remainder() -> None:
    text = "word " * 1000
    first = format_page_text(
        title="Long", url="https://example.com", text=text, offset=0, max_chars=100
    )
    second = format_page_text(
        title="Long", url="https://example.com", text=text, offset=100, max_chars=100
    )

    assert "<<UNTRUSTED_PAGE_CONTENT>>" in first
    assert "Call read_page with offset=100" in first
    assert "continuing from character 100" in second


def test_lead_text_keeps_words_whole() -> None:
    lead = lead_text("alpha beta gamma delta", limit=12)
    assert lead.startswith("alpha beta")
    assert lead.endswith("...")
