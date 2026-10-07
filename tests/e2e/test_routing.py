"""Routing backends, driven through the real app and browser.

The tier's default fixture stubs the router probe out, so it only ever sees
the degraded estimates path. These tests add local look-alikes of the two real
backends -- Valhalla and OpenRouteService -- so the full probe-then-route chain
runs against a real HTTP server for each. That is the safety net a modular
refactor of the routing layer needs: no other test drives both backends
through the wire protocol the server actually returns.
"""

from __future__ import annotations


def _search_and_walk(page) -> str:
    """Type the auto-selecting address and return the winner's walk text."""
    q = page.locator("#q")
    q.fill("Marsel yanko 10")
    q.press("Enter")
    card = page.locator("#winner .walk")
    card.wait_for(timeout=10_000)
    return card.text_content()


def test_no_router_shows_the_estimate_banner(page) -> None:
    """The degraded state is visible: a tilde on the walk and a warning."""
    text = _search_and_walk(page)
    assert "~" in text, f"expected an estimate marker, got {text!r}"

    status = page.locator("#status")
    status.wait_for(state="visible")
    message = status.text_content()
    assert "Routing server is not running" in message, message


def test_a_reachable_valhalla_yields_exact_walk_times(page_valhalla) -> None:
    """A live probe finds the Valhalla look-alike and still hides the banner."""
    text = _search_and_walk(page_valhalla)
    assert "min walk" in text
    assert "~" not in text, f"routed distances must not carry the marker: {text!r}"

    page_valhalla.locator("#status").wait_for(state="hidden")


def test_ors_takes_over_when_valhalla_is_unreachable(page_ors) -> None:
    """Dead Valhalla hosts fall through to ORS through the real selection code.

    VALHALLA_URLS points at a refused port; the ORS directions look-alike is
    what the responses come from, so this exercises probe selection, the ORS
    GET wire format, and the ORS response parse end to end.
    """
    text = _search_and_walk(page_ors)
    assert "min walk" in text
    assert "~" not in text, f"ors-routed distances must not carry the marker: {text!r}"

    page_ors.locator("#status").wait_for(state="hidden")