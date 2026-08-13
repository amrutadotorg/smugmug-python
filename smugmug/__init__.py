"""SmugMug API v2 client library."""

from smugmug.client import RateLimitError, SmugMugClient, SmugMugError

__version__ = "1.1.1"

__all__ = ["RateLimitError", "SmugMugClient", "SmugMugError", "__version__"]
