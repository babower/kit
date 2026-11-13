# kit-charts

Dependency-free SVG charts and a shared, validated palette for server-rendered dashboards:
stacked column charts, line charts and horizontal bars, with tooltips (mouse and keyboard),
a table view for every chart, light/dark mode with a toggle, and narrower drawings on
phones. No build step, no CDN, and no inline style attributes, so it works under a strict
Content-Security-Policy.

```bash
pip install "kit-charts @ git+https://github.com/babower/kit@charts-v0.1.0#subdirectory=python/charts"
```

## Use it

Inline both files in the page (Jinja, with autoescaping):

```python
import kit_charts
html = template.render(charts_css=kit_charts.css_markup(), charts_js=kit_charts.js_markup(), ...)
```

```html
<style nonce="{{ nonce }}">{{ charts_css }} /* then your layout */</style>
...
<div id="tip" role="tooltip"></div>
<button id="theme" type="button">Theme</button>            <!-- optional -->
<script nonce="{{ nonce }}">
  window.KIT_CHARTS = { themeKey: "my-app-theme" };      // optional
  {{ charts_js }}
  const D = JSON.parse(document.getElementById("data").textContent);
  window.renderAll = function () {
    $("spend").replaceChildren();
    columnChart($("spend"), { labels: D.days, series: [{ label: "Claude", color: seriesColor(0),
      values: D.claude }], fmt: (v) => money(v) });
  };
  window.renderAll();
</script>
```

Or, for a static page, `kit_charts.js()` and `kit_charts.css()` return plain strings.

`window.renderAll` is called again when the theme or screen width changes, so charts pick up
the new colours and size. Draw from data, never from markup.

## What's in it

| Function | Draws |
| --- | --- |
| `columnChart(el, {labels, series, fmt, height, tickFmt, extraBelow, width})` | Columns per label, stacked when there are several series; `extraBelow` adds markers under columns |
| `lineChart(el, {labels, values, fmt, color, height, title})` | One series as a line with points; gaps for nulls |
| `hbars(el, rows, {color, fmt, label, note, max})` | Horizontal bars with labels and notes |
| `legend(items)`, `table(headers, rows)`, `tableView(headers, rows)` | A legend; a table; a table behind "Show table" |
| `statusChip(cls, text)`, `empty(el, text)` | A coloured status (`good`, `warn`, `bad`, `none`); an empty-state line |
| `money`, `num`, `pct`, `compact`, `shortDay`, `when`, `plural`, `seriesColor(i)`, `css(var)`, `h()`, `s()`, `$()` | Formatting and DOM helpers |

Series colours come from `seriesColor(i)`: the six palette colours in a fixed order, then
neutral grey. Keep a series on the same index everywhere so its colour never changes.

`charts.css` defines the tokens (`--surface-0/1`, `--border`, `--grid`, `--text-*`,
`--series-1..6`, `--neutral`, status colours) for light and dark, plus the chart, legend,
tooltip, table, status, meter and KPI-tile styles. Your page adds its own layout.
