"""Exception types shared across the application."""


class DocuShelfError(Exception):
    """Base class for all application errors."""


class ConfigError(DocuShelfError):
    """Invalid or incomplete configuration."""


class PathError(DocuShelfError):
    """A path is unsafe (traversal, absolute, hidden) or otherwise invalid."""


class NotFound(DocuShelfError):
    """A document, folder or inbox item does not exist (anymore)."""


class Conflict(DocuShelfError):
    """The file system changed underneath an operation, or a target already exists."""


class ProcessingError(DocuShelfError):
    """A pipeline step failed.

    ``transient`` marks errors that may succeed on retry (timeouts, locked files).
    """

    def __init__(self, message: str, *, stage: str | None = None, transient: bool = False):
        super().__init__(message)
        self.stage = stage
        self.transient = transient


class UnsupportedFile(ProcessingError):
    """The file type cannot be turned into a PDF."""


class AuthError(DocuShelfError):
    """Authentication or authorization failed."""
