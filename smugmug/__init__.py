"""SmugMug API v2 client library."""

from smugmug.client import RateLimitError, SmugMugClient, SmugMugError

__version__ = "1.1.0"

__all__ = ["RateLimitError", "SmugMugClient", "SmugMugError", "__version__"]
