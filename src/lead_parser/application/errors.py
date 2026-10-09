"""Failures crossing the adapter boundary contain no transport objects or secrets."""


class DeliveryError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False, retry_after: float = 0):
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


class StorageError(RuntimeError):
    pass
