__package__ = 'abx_pkg'


# Unfortunately it must be kept in the same file as BinProvider because of the circular type reference between them
from .binprovider import ShallowBinary      # noqa: F401
