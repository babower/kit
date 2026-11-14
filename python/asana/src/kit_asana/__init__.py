"""kit-asana: a small, safe Asana REST client (standard library only).

- `Client`: create, update, complete, comment on and read tasks; paginated project reads.
- Errors: `AsanaError` (status, safe message), `AsanaAuthError`, `AsanaNotFound`.
- `kit_asana.testing.FakeAsana`: a local fake Asana for an app's own tests.
"""

from ._version import __version__
from .client import AsanaAuthError, AsanaError, AsanaNotFound, Client

__all__ = ["AsanaAuthError", "AsanaError", "AsanaNotFound", "Client", "__version__"]
