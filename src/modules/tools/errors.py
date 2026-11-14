"""Errors named in the supplied tool contracts (data/tools.md) + the circuit-breaker fast-fail."""


class CustomerNotFound(Exception):
    pass


class UpstreamTimeout(Exception):
    pass


class InvalidCategory(Exception):
    pass


class CircuitOpen(Exception):
    pass
