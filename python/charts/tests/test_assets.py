"""The assets ship with the package, stay CSP-safe, and keep the validated palette."""

import re
import shutil
import subprocess

import pytest

import kit_charts

PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"]


def test_assets_are_shipped():
    assert "function columnChart" in kit_charts.js()
    assert "svg.chart" in kit_charts.css()
    assert str(kit_charts.js_markup()) == kit_charts.js()  # Markup is not escaped again


def test_never_sets_a_style_attribute():
    # A CSP without 'unsafe-inline' blocks style attributes; element.style is fine.
    js = kit_charts.js()
    assert not re.search(r"""setAttribute\(\s*["']style""", js)
    assert not re.search(r"""\{\s*style\s*:""", js)  # h("div", { style: ... })


def test_safe_to_inline_in_a_page():
    # Inlined text must never look like a tag: "</script" would end the script early.
    for text in (kit_charts.js(), kit_charts.css()):
        lowered = text.lower()
        assert "<script" not in lowered and "</script" not in lowered
        assert "<style" not in lowered and "</style" not in lowered


def test_reads_no_page_data_and_needs_no_network():
    js = kit_charts.js()
    assert 'getElementById("data")' not in js  # each page parses its own data
    assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", js)  # no CDN or fetches


def test_the_palette_stays_as_validated_and_in_order():
    css = kit_charts.css()
    light = re.findall(r"--series-\d: (#[0-9a-f]{6})", css.split("@media")[0])
    assert light == PALETTE_LIGHT
    dark = re.findall(r"--series-\d: (#[0-9a-f]{6})", css.split(':root[data-theme="dark"]')[1])
    assert dark == PALETTE_DARK


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_javascript_parses():
    subprocess.run(["node", "--check", "-"], input=kit_charts.js(), text=True, check=True)
