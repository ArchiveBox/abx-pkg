__package__ = "abxpkg"


# ShallowBinary must live in binprovider.py because of the circular type
# reference between it and BinProvider. This module is only a stable import shim.
from .binprovider import ShallowBinary  # noqa: F401
