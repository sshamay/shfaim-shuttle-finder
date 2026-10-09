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


def test_picking_a_street_keeps_the_typed_house_number(page) -> None:
    """A street-only pick must not drop the number the user typed.

    The stub answers "Bialik 7" with two street candidates and no number, so
    the list appears. Clicking one triggers the refine: the UI re-asks for
    "<street> <number>, <city>", and only a local numbered match on the same
    street may replace the street point.
    """
    q = page.locator("#q")
    q.fill("Bialik 7")
    q.press("Enter")

    # Two local street answers: the list is offered, nothing auto-selects.
    items = page.locator("#candidates li")
    items.first.wait_for(timeout=10_000)
    assert items.count() == 2

    items.first.click()

    # The refined house replaced the street point: the box now carries the
    # full address and the results came from the house coordinates.
    page.locator("#winner .walk").wait_for(timeout=10_000)
    assert page.input_value("#q") == "Bialik 7, Tel Aviv"
    assert page.evaluate("() => map.getBounds().contains([32.0725, 34.778])")


def test_results_can_be_hidden_and_restored(page) -> None:
    """The panel folds up so the map gets the screen back, with a visible undo.

    The toggle lives outside the results panel, so hiding the panel must never
    hide the way back; the mobile pill is only a second, thumb-friendly path.
    """
    page.set_viewport_size({"width": 390, "height": 844})

    q = page.locator("#q")
    q.fill("Marsel yanko 10")
    q.press("Enter")

    page.locator("#winner .walk").wait_for(timeout=10_000)

    toggle = page.locator("#collapseBtn")
    toggle.wait_for(state="visible")
    assert toggle.text_content() == "Hide results ▼"

    # Collapse: the panel goes away but the toggle must stay and flip label.
    toggle.click()
    page.locator("#result").wait_for(state="hidden")
    assert toggle.text_content() == "Show results ▲"

    # The fixed map now owns the full viewport width again.
    box = page.locator("#map").bounding_box()
    assert box is not None and box["width"] == 390

    # Undo via the toggle itself.
    toggle.click()
    page.locator("#result").wait_for(state="visible")
    page.locator("#resultPill").wait_for(state="hidden")
    assert toggle.text_content() == "Hide results ▼"

    # Collapse again and undo via the floating pill on phones.
    toggle.click()
    page.locator("#result").wait_for(state="hidden")
    page.locator("#resultPill").wait_for(state="visible")
    pill = page.locator("#resultPill")
    text = pill.text_content()
    assert "811" in text and "min walk" in text, text

    pill.click()
    page.locator("#result").wait_for(state="visible")
    page.locator("#resultPill").wait_for(state="hidden")


def test_collapse_toggle_is_visible_on_desktop(page) -> None:
    """The hide/results toggle is not a mobile-only control."""
    q = page.locator("#q")
    q.fill("Marsel yanko 10")
    q.press("Enter")

    page.locator("#winner .walk").wait_for(timeout=10_000)

    toggle = page.locator("#collapseBtn")
    toggle.wait_for(state="visible")
    toggle.click()
    page.locator("#result").wait_for(state="hidden")
    assert toggle.text_content() == "Show results ▲"

    toggle.click()
    page.locator("#result").wait_for(state="visible")