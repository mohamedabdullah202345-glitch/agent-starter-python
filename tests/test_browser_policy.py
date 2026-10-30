import pytest

from browser.policy import (
    BrowserPolicyError,
    assert_url_allowed,
    host_in_allowlist,
    is_risky_action,
    normalize_url,
)


def test_normalize_adds_scheme_and_keeps_path() -> None:
    assert normalize_url("example.com/a?b=1") == "https://example.com/a?b=1"
    assert normalize_url("  https://example.com  ") == "https://example.com"


@pytest.mark.parametrize(
    "url",
    ["file:///C:/Windows/win.ini", "javascript:alert(1)", "data:text/html,hi"],
)
def test_normalize_rejects_dangerous_schemes(url: str) -> None:
    with pytest.raises(BrowserPolicyError):
        normalize_url(url)


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://printer.local"])
def test_normalize_rejects_local_hosts(url: str) -> None:
    with pytest.raises(BrowserPolicyError):
        normalize_url(url)


def test_normalize_rejects_embedded_credentials() -> None:
    with pytest.raises(BrowserPolicyError):
        normalize_url("https://user:secret@example.com")


def test_allowlist_matches_subdomains() -> None:
    assert host_in_allowlist("docs.example.com", ("example.com",))
    assert not host_in_allowlist("example.com.evil.test", ("example.com",))


async def test_private_addresses_blocked_by_default() -> None:
    with pytest.raises(BrowserPolicyError):
        await assert_url_allowed("http://127.0.0.1:8000/index.html")

    assert (
        await assert_url_allowed(
            "http://127.0.0.1:8000/index.html", allow_private_hosts=True
        )
        == "http://127.0.0.1:8000/index.html"
    )


async def test_allowlist_blocks_other_domains() -> None:
    with pytest.raises(BrowserPolicyError):
        await assert_url_allowed(
            "https://example.com", allowlist=("livekit.io",), allow_private_hosts=True
        )


@pytest.mark.parametrize("name", ["Buy now", "Place order", "Delete account", "Submit"])
def test_risky_names_are_gated(name: str) -> None:
    assert is_risky_action(name)


@pytest.mark.parametrize("name", ["Search", "Next page", "Learn more", None])
def test_safe_names_are_not_gated(name: str | None) -> None:
    assert not is_risky_action(name)
