"""opsreport: send usage, runs and outcomes to the ops dashboard.

    from opsreport import Reporter

    ops = Reporter("social-media-prayer-flags")   # reads OPS_URL and OPS_TOKEN
    ops.usage(provider="anthropic", op="write", model="claude-opus-5-5",
              input_tokens=900, output_tokens=300, cost_usd=0.0096)
    ops.flush()

Nothing here ever raises into your code, and if OPS_URL or OPS_TOKEN is unset it does
nothing at all.
"""

from .reporter import FlushResult, Reporter

__all__ = ["FlushResult", "Reporter", "__version__"]
from ._version import __version__
