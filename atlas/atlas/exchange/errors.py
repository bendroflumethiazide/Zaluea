"""Exchange errors shared by the fake and real exchange clients.

The gateway relies on these meaning exactly one thing each:

  OrderRejected     the exchange definitely did NOT accept the order.
  DuplicateOrder    an order with this client_order_id already exists: look it up.
  AuthError         credentials invalid, revoked or lacking a scope: stop trading.
  TimeoutError,
  ConnectionError,
  ExchangeError     the outcome is UNKNOWN: the order may or may not exist.
  DataError         the exchange returned something ATLAS cannot account for
                    exactly (e.g. a fractional fill): treat as an integrity problem.
"""


class ExchangeError(Exception):
    """A request failed in a way that leaves its outcome unknown."""


class DuplicateOrder(ExchangeError):
    pass


class OrderRejected(ExchangeError):
    """Definitive: the order was not placed."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class AuthError(ExchangeError):
    pass


class DataError(ExchangeError):
    pass
