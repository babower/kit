"""kit-charts: dependency-free SVG charts and the shared palette, for server-rendered pages.

    import kit_charts
    kit_charts.css()   # tokens and component styles: put inside <style nonce="...">
    kit_charts.js()    # the chart helpers: put inside <script nonce="..."> before page code

No build step, no CDN, no inline style attributes (so it works under a strict CSP). In a
Jinja template with autoescaping, pass them as Markup (`kit_charts.css_markup()`), or
the browser receives escaped text.
"""

from __future__ import annotations

from functools import cache
from importlib import resources

__version__ = "0.1.0"


@cache
def js() -> str:
    return resources.files(__name__).joinpath("charts.js").read_text("utf-8")


@cache
def css() -> str:
    return resources.files(__name__).joinpath("charts.css").read_text("utf-8")


def js_markup():
    """`js()` as markupsafe.Markup, for autoescaping templates (needs markupsafe)."""
    from markupsafe import Markup

    return Markup(js())


def css_markup():
    """`css()` as markupsafe.Markup, for autoescaping templates (needs markupsafe)."""
    from markupsafe import Markup

    return Markup(css())


__all__ = ["__version__", "css", "css_markup", "js", "js_markup"]
