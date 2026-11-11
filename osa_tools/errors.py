"""Exception type for failures that are reported without a traceback."""


class OsaError(Exception):
    """A failure the operator can act on; the message is printed as is."""
