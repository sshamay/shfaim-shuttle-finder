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

    # The winner (line 811, the Abba Hillel stop) has its own card; "Other
    # lines" must not repeat it. The 815 suggestion is the remaining row.
    other_lines = page.locator("#lines .line-no").all_inner_texts()
    assert "815" in other_lines, f"expected 815 as an alternative, got {other_lines}"
    assert "811" not in other_lines, f"winner repeated in Other lines: {other_lines}"

    # The map was centred on the answer and stop pins were dropped.
    pins = page.locator(".leaflet-marker-pane .pin")
    pins.first.wait_for(timeout=10_000)
    assert pins.count() >= 1

    # The box kept the original misspelling: the whole round-trip went through
    # the real /api/search -> auto-select -> /api/nearest -> render path.
    assert page.input_value("#q") == "Marsel yanko 10"


def test_results_collapse_to_a_pill_on_mobile(page) -> None:
    """On a phone the results panel folds up so the map gets the screen back."""
    page.set_viewport_size({"width": 390, "height": 844})

    q = page.locator("#q")
    q.fill("Marsel yanko 10")
    q.press("Enter")

    page.locator("#winner .walk").wait_for(timeout=10_000)

    # "Hide" is a mobile-only control and the panel collapses.
    page.locator("#collapseBtn").wait_for(state="visible")
    page.locator("#collapseBtn").click()
    page.locator("#result").wait_for(state="hidden")
    page.locator("#resultPill").wait_for(state="visible")

    pill = page.locator("#resultPill")
    text = pill.text_content()
    assert "811" in text and "min walk" in text, text

    # The fixed map now owns the full viewport width again.
    box = page.locator("#map").bounding_box()
    assert box is not None and box["width"] == 390

    # Tapping the pill brings the full results back.
    pill.click()
    page.locator("#result").wait_for(state="visible")
    page.locator("#resultPill").wait_for(state="hidden")