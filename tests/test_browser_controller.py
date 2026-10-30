import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from browser.controller import BrowserController, BrowserError, BrowserSettings
from browser.policy import BrowserPolicyError

FIXTURES = Path(__file__).parent / "fixtures" / "site"


class _QuietHandler(SimpleHTTPRequestHandler):
    paths: ClassVar[list[str]] = []

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        super().do_GET()


@pytest.fixture(scope="session")
def site_url():
    handler = partial(_QuietHandler, directory=str(FIXTURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
async def browser():
    controller = BrowserController(
        BrowserSettings(
            headless=True,
            allow_private_hosts=True,
            settle_timeout_ms=400,
        )
    )
    yield controller
    await controller.aclose()


def _by_name(view, name: str):
    return next(ref for ref in view.refs if ref.name == name)


async def test_open_inspect_and_submit_form(browser, site_url) -> None:
    view = await browser.navigate(f"{site_url}/index.html")

    assert view.title == "Fixture Home"
    assert "Welcome to the fixture site" in view.text
    assert {"Search", "Buy now", "Open the form"} <= {ref.name for ref in view.refs}

    result = await browser.click(_by_name(view, "Open the form").ref)
    assert "Fixture Form" in result

    form = await browser.inspect()
    typed = await browser.type_text(_by_name(form, "Name").ref, "Alex")
    assert "Typed into" in typed

    submitted = await browser.click(_by_name(form, "Submit").ref)
    assert "Fixture Thanks" in submitted

    page_text = await browser.read()
    assert "fixture form was submitted" in page_text


async def test_risky_controls_are_described_for_confirmation(browser, site_url) -> None:
    view = await browser.navigate(f"{site_url}/index.html")

    assert browser.describe_click(_by_name(view, "Buy now").ref) == (
        'Click "Buy now" on 127.0.0.1'
    )
    assert browser.describe_click(_by_name(view, "Search").ref) is None


async def test_submitting_a_form_needs_confirmation_but_a_search_does_not(
    browser, site_url
) -> None:
    view = await browser.navigate(f"{site_url}/index.html")
    described = browser.describe_submit(_by_name(view, "Search").ref, "cats")
    assert described is None

    await browser.click(_by_name(view, "Open the form").ref)
    form = await browser.inspect()
    described = browser.describe_submit(_by_name(form, "Name").ref, "Alex")
    assert described is not None
    assert "Alex" in described
    assert described.endswith("and submit the form")


async def test_expired_confirmation_is_rejected() -> None:
    controller = BrowserController(BrowserSettings(confirmation_ttl=-1))
    token = controller.stash("Do the thing", _never)
    with pytest.raises(BrowserError, match="expired"):
        await controller.confirm(token)


async def test_pending_actions_are_capped() -> None:
    controller = BrowserController(BrowserSettings(max_pending=2))
    first = controller.stash("One", _never)
    second = controller.stash("Two", _never)
    third = controller.stash("Three", _never)

    assert first not in controller._pending
    assert second in controller._pending
    assert third in controller._pending


async def _never() -> str:
    raise AssertionError("should not run")


async def test_confirm_runs_once(browser) -> None:
    calls: list[str] = []

    async def action() -> str:
        calls.append("ran")
        return "done"

    token = browser.stash("Do the thing on example.com", action)
    assert await browser.confirm(token) == "done"
    assert calls == ["ran"]

    with pytest.raises(BrowserError):
        await browser.confirm(token)


async def test_blocks_file_urls(browser) -> None:
    with pytest.raises(BrowserPolicyError):
        await browser.navigate("file:///C:/Windows/win.ini")


async def test_unknown_ref_is_rejected(browser, site_url) -> None:
    await browser.navigate(f"{site_url}/index.html")
    with pytest.raises(BrowserError):
        await browser.click("e99")


def test_page_audio_and_autoplay_are_disabled() -> None:
    args = BrowserController()._launch_args()

    assert "--mute-audio" in args
    assert "--autoplay-policy=user-gesture-required" in args


def test_media_is_blocked_by_default() -> None:
    assert BrowserSettings().block_media is True


async def test_images_are_not_downloaded(browser, site_url) -> None:
    _QuietHandler.paths.clear()

    view = await browser.navigate(f"{site_url}/media.html")

    assert "Continue" in {ref.name for ref in view.refs}
    assert "/pixel.png" not in _QuietHandler.paths


async def test_page_that_paints_late_is_inspected_again(browser, site_url) -> None:
    view = await browser.navigate(f"{site_url}/late.html")

    assert "Reveal later" in {ref.name for ref in view.refs}
