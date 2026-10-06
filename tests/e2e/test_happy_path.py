"""End-to-end happy path: type a street address, land on the map.

Drives the real FastAPI app through a real browser against stubbed data
(see ``tests/e2e/conftest.py``), so it needs network neither for geocoding nor
for tile images while still exercising the actual HTML, JavaScript, and HTTP
shape the user gets.
"""

from __future__ import annotations


def test_typing_a_street_address_resolves_straight_to_the_map(page) -> None:
    """A lone local address auto-selects: no picker, results on the map.

    The stub returns the Marcel Janco house, a same-number butcher, and an
    out-of-area homonym. Exactly one local address-type candidate must win
    without asking, which is the behaviour added for "Marsel yanko 10".
    """
    q = page.locator("#q")
    q.fill("Marsel yanko 10")
    q.press("Enter")

    # The address auto-selected, so no candidate list was ever offered.
    assert page.locator("#candidates li").count() == 0

    # The best-stop card rendered with a walk time.
    card = page.locator("#winner .walk")
    card.wait_for(timeout=10_000)
    text = card.text_content()
    assert "min walk" in text
    assert " m" in text

    # The stops panel and per-line table are both populated.
    page.locator("#result").wait_for(state="visible")
    assert page.locator("#lines tr").count() >= 1
    assert page.locator("#stops tr").count() >= 1

    # The map was centred on the answer and stop pins were dropped.
    pins = page.locator(".leaflet-marker-pane .pin")
    pins.first.wait_for(timeout=10_000)
    assert pins.count() >= 1

    # The box kept the original misspelling: the whole round-trip went through
    # the real /api/search -> auto-select -> /api/nearest -> render path.
    assert page.input_value("#q") == "Marsel yanko 10"