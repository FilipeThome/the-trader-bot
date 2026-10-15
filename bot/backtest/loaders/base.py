"""Loader errors the ported enforcement gate already understands."""


class NoAvailableSourceError(LookupError):
    """No public source could satisfy the request."""


class DataLoader:
    name = "base"
    markets: set[str] = set()
