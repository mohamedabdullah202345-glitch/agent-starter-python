"""Turn an ARIA snapshot into the compact element list the model can act on."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Roles that can be clicked or typed into. Everything else is page structure and
# is left out to keep the tool result small.
_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "option",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "switch",
        "slider",
        "spinbutton",
        "treeitem",
    }
)

_LINE = re.compile(
    r"^(?P<indent>\s*)-\s+(?P<role>[A-Za-z]+)"
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r"(?P<rest>.*)$"
)
_URL = re.compile(r"^\s*-\s*/url:\s*(?P<url>\S+)\s*$")
_VALUE = re.compile(r"^\s*-\s*/value:\s*(?P<value>.*)$")
_ATTR = re.compile(r"\[([^\]]*)\]")

TEXT_EXTRACTION_SCRIPT = """
() => {
  const preferred = document.querySelector('main, article, [role="main"]');
  const root = preferred || document.body;
  return root ? root.innerText : '';
}
"""


@dataclass(frozen=True)
class ElementRef:
    """A stable handle for one interactive element on the current page."""

    ref: str
    role: str
    name: str
    nth: int
    page_url: str
    disabled: bool = False
    href: str | None = None
    value: str | None = None

    def describe(self) -> str:
        parts = [f'[{self.ref}] {self.role} "{self.name}"']
        if self.disabled:
            parts.append("(disabled)")
        if self.value:
            parts.append(f"(value: {self.value[:60]})")
        if self.href:
            parts.append(f"-> {self.href[:120]}")
        return " ".join(parts)


@dataclass
class SnapshotResult:
    refs: list[ElementRef] = field(default_factory=list)
    truncated: bool = False


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def parse_snapshot(
    snapshot: str,
    *,
    page_url: str,
    limit: int = 60,
) -> SnapshotResult:
    """Parse an ARIA snapshot and assign ``e1..eN`` refs to interactive nodes."""
    result = SnapshotResult()
    counts: dict[tuple[str, str], int] = {}
    entries: list[dict[str, object]] = []

    for line in snapshot.splitlines():
        url_match = _URL.match(line)
        if url_match:
            if entries:
                entries[-1]["href"] = url_match.group("url")
            continue

        value_match = _VALUE.match(line)
        if value_match:
            if entries:
                entries[-1]["value"] = value_match.group("value").strip()
            continue

        match = _LINE.match(line)
        if not match:
            continue

        role = match.group("role").lower()
        if role not in _INTERACTIVE_ROLES:
            continue

        raw_name = match.group("name")
        if raw_name is None:
            continue
        name = _unescape(raw_name).strip()
        if not name:
            continue

        rest = match.group("rest") or ""
        head = rest.split(":", 1)[0]
        attrs = " ".join(_ATTR.findall(head)).lower()
        if len(entries) >= limit:
            result.truncated = True
            break

        key = (role, name.lower())
        entries.append(
            {
                "role": role,
                "name": name,
                "nth": counts.get(key, 0),
                "disabled": "disabled" in attrs,
                "href": None,
                "value": None,
            }
        )
        counts[key] = counts.get(key, 0) + 1

    for index, entry in enumerate(entries, start=1):
        result.refs.append(
            ElementRef(
                ref=f"e{index}",
                role=str(entry["role"]),
                name=str(entry["name"]),
                nth=int(entry["nth"]),  # type: ignore[arg-type]
                page_url=page_url,
                disabled=bool(entry["disabled"]),
                href=entry["href"] if isinstance(entry["href"], str) else None,
                value=entry["value"] if isinstance(entry["value"], str) else None,
            )
        )

    return result


def format_elements(refs: list[ElementRef], *, truncated: bool = False) -> str:
    if not refs:
        return "No clickable or typeable elements were found on this page."

    lines = [ref.describe() for ref in refs]
    if truncated:
        lines.append("(List cut short. Scroll or narrow the page, then inspect again.)")
    return "\n".join(lines)


_PAGE_HEADER = "<<UNTRUSTED_PAGE_CONTENT>>"
_PAGE_FOOTER = "<<END_UNTRUSTED_PAGE_CONTENT>>"
_WHITESPACE = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def lead_text(text: str, limit: int = 320) -> str:
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return f"{cut}..."


def format_page_text(
    *,
    title: str,
    url: str,
    text: str,
    offset: int = 0,
    max_chars: int = 3000,
) -> str:
    text = normalize_text(text)
    total = len(text)
    offset = max(0, min(offset, total))
    chunk = text[offset : offset + max_chars]
    end = offset + len(chunk)

    header = f'Page: "{title}" ({url})'
    if offset:
        header += f", continuing from character {offset}"
    parts = [
        header,
        _PAGE_HEADER,
        chunk or "(The page has no readable text.)",
        _PAGE_FOOTER,
    ]
    if end < total:
        parts.append(
            f"[{end} of {total} characters shown. "
            f"Call read_page with offset={end} for the rest.]"
        )
    return "\n".join(parts)


__all__ = [
    "TEXT_EXTRACTION_SCRIPT",
    "ElementRef",
    "SnapshotResult",
    "format_elements",
    "format_page_text",
    "lead_text",
    "normalize_text",
    "parse_snapshot",
]
