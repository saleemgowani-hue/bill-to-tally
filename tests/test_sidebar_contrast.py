"""Nothing in the sidebar may be light text on a light background.

The sidebar paints every descendant near-white, because it sits on a dark blue
gradient:

    section[data-testid="stSidebar"] * { color: #e8f1f8; }

That is right for plain text and wrong for anything carrying its own pale
background. Three elements did, and all three went invisible: the licence chip
(light green on light green), the inline `code` chip after "OCR provider:", and
the **Sign out** button, which is a white Streamlit button and so was rendering
white text on white.

These tests read the colours out of the stylesheet and do the WCAG contrast
arithmetic, so a future edit that drops an override fails here rather than on a
customer's screen. The threshold is 4.5:1, the WCAG AA minimum for body text.
"""
import re

import pytest

from utils.ui import CSS

# The sidebar gradient, darkest to lightest. A translucent fill is blended over
# the lightest stop, which is the worst case for white text.
SIDEBAR_STOPS = ["#0b2e4a", "#103d63", "#0b4a6f"]
AA_NORMAL = 4.5


# --------------------------------------------------------------------------
# colour arithmetic (WCAG 2.1)
# --------------------------------------------------------------------------
def parse_colour(value: str):
    """'#0b4a6f' or 'rgba(255,255,255,.14)' -> (r, g, b, a)."""
    value = value.strip()
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), 1.0)
    match = re.match(r"rgba?\(([^)]+)\)", value)
    if not match:
        raise ValueError(f"cannot parse colour: {value!r}")
    parts = [p.strip() for p in match.group(1).split(",")]
    r, g, b = (int(float(p)) for p in parts[:3])
    alpha = float(parts[3]) if len(parts) > 3 else 1.0
    return (r, g, b, alpha)


def blend(foreground, background):
    """Composite a translucent colour over an opaque one."""
    fr, fg, fb, alpha = foreground
    br, bg, bb, _ = background
    return (
        round(fr * alpha + br * (1 - alpha)),
        round(fg * alpha + bg * (1 - alpha)),
        round(fb * alpha + bb * (1 - alpha)),
        1.0,
    )


def relative_luminance(colour):
    channels = []
    for value in colour[:3]:
        srgb = value / 255
        channels.append(
            srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground, background) -> float:
    fore = blend(parse_colour(foreground), parse_colour(background)) \
        if parse_colour(foreground)[3] < 1 else parse_colour(foreground)
    back = parse_colour(background)
    if back[3] < 1:
        back = blend(back, parse_colour(SIDEBAR_STOPS[-1]))
    lighter, darker = sorted(
        (relative_luminance(fore), relative_luminance(back)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


# --------------------------------------------------------------------------
# reading declarations out of the stylesheet
# --------------------------------------------------------------------------
#: the stylesheet with /* comments */ removed, so prose never looks like a
#: selector to the parser below
BARE_CSS = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)


def rules_matching(selector_fragment: str):
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", BARE_CSS):
        selector = " ".join(match.group(1).split())
        if selector_fragment in selector:
            yield selector, match.group(2)


def value_of(selector_fragment: str, property_name: str, *,
             sidebar_only: bool = False, skip=()) -> str:
    """The value this property ends up with, reading rules in source order.

    `sidebar_only` picks the sidebar-scoped rule, which is more specific and so
    what actually renders there. `skip` leaves out states and inner elements
    (`:hover`, `button p`) when the base rule is the one being measured.
    """
    hits = []
    for selector, block in rules_matching(selector_fragment):
        if sidebar_only and "stSidebar" not in selector:
            continue
        if any(fragment in selector for fragment in skip):
            continue
        match = re.search(rf"(?:^|;)\s*{property_name}\s*:\s*([^;!]+)", block)
        if match:
            hits.append(match.group(1).strip())
    assert hits, (
        f"no rule for {selector_fragment!r} sets `{property_name}`"
    )
    return hits[-1]


# States and inner elements, so these read the button's own base rule.
SKIP_STATES = (":hover", "button p", "button span", "button div")


# Looked up lazily: a missing rule should fail the test that needs it, not
# blow up at import and take the whole file down with it.
def signout_colour() -> str:
    return value_of("st-key-signout", "color", skip=SKIP_STATES)


def signout_fill() -> str:
    return value_of("st-key-signout", "background", skip=SKIP_STATES)


class TestColourMaths:
    """The helpers above, checked against values with known answers."""

    def test_black_on_white_is_the_maximum(self):
        assert contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)

    def test_white_on_white_is_the_minimum(self):
        assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)

    def test_the_old_bug_would_have_failed(self):
        """White-ish sidebar text on the licence chip's pale green fill."""
        assert contrast("#e8f1f8", "#d1fadf") < AA_NORMAL

    def test_translucent_white_lightens_a_dark_ground(self):
        blended = blend(parse_colour("rgba(255,255,255,.10)"),
                        parse_colour("#0b4a6f"))
        assert relative_luminance(blended) > relative_luminance(parse_colour("#0b4a6f"))


class TestLicenceChip:
    @pytest.mark.parametrize("css_class", ["lic-ok", "lic-warn", "lic-bad"])
    def test_chip_text_is_readable_on_its_own_fill(self, css_class):
        """The colour the sidebar override lands on, over the chip's own fill."""
        ratio = contrast(
            value_of(f".{css_class}", "color", sidebar_only=True),
            value_of(f".{css_class}", "background"),
        )
        assert ratio >= AA_NORMAL, f".{css_class} is {ratio:.2f}:1"

    @pytest.mark.parametrize("css_class", ["lic-ok", "lic-warn", "lic-bad"])
    def test_the_sidebar_override_exists(self, css_class):
        """Without this the blanket rule repaints the chip and it vanishes."""
        assert any(True for _ in rules_matching(f'stSidebar"] .{css_class}')), (
            f"the sidebar does not restore .{css_class}'s colour"
        )


class TestInlineCode:
    def test_code_chip_is_readable(self):
        ratio = contrast(
            value_of('stSidebar"] code', "color"),
            value_of('stSidebar"] code', "background"),
        )
        assert ratio >= AA_NORMAL, f"sidebar code is {ratio:.2f}:1"


class TestSignOutButton:
    def test_it_is_styled_at_all(self):
        assert 'st-key-signout' in CSS, (
            "Sign out falls back to a white Streamlit button, and the blanket "
            "sidebar rule paints its label white — invisible"
        )

    def test_its_label_is_readable(self):
        ratio = contrast(signout_colour(), signout_fill())
        assert ratio >= AA_NORMAL, f"Sign out is {ratio:.2f}:1"

    def test_it_is_readable_on_every_gradient_stop(self):
        """The sidebar is a gradient; the button must work the whole way down."""
        fill = parse_colour(signout_fill())
        for stop in SIDEBAR_STOPS:
            ground = blend(fill, parse_colour(stop))
            rgb = f"rgb({ground[0]},{ground[1]},{ground[2]})"
            ratio = contrast(signout_colour(), rgb)
            assert ratio >= AA_NORMAL, f"{ratio:.2f}:1 over {stop}"

    def test_its_inner_text_is_forced_too(self):
        """Streamlit wraps the label in its own element, which the blanket
        rule would otherwise catch on its way down."""
        assert any(True for _ in rules_matching('st-key-signout"] button p')), (
            "the label element itself needs the colour, not just the button"
        )

    def test_it_does_not_fight_the_navigation_buttons(self):
        """Keyed by name, not by `.stButton`, so the coloured nav buttons —
        which set their own background with !important — are untouched."""
        for match in re.finditer(r"([^{}]+)\{[^{}]*\}", BARE_CSS):
            selector = match.group(1)
            if "stSidebar" in selector and ".stButton" in selector:
                pytest.fail(
                    "a blanket .stButton rule in the sidebar would collide "
                    f"with the nav buttons: {selector.strip()[:100]}"
                )


class TestPlainSidebarTextStillWorks:
    def test_the_blanket_rule_is_still_there(self):
        """The overrides above assume it. If it goes, they are dead weight and
        the sidebar's ordinary text turns dark on dark."""
        assert 'section[data-testid="stSidebar"] * { color: #e8f1f8; }' in CSS

    def test_ordinary_sidebar_text_is_readable(self):
        for stop in SIDEBAR_STOPS:
            ratio = contrast("#e8f1f8", stop)
            assert ratio >= AA_NORMAL, f"{ratio:.2f}:1 over {stop}"
