from typing import Callable

from django.conf import settings
from django.utils.module_loading import import_string


ABX_PKG_GET_ALL_BINARIES  = getattr(settings, 'ABX_PKG_GET_ALL_BINARIES', 'abx_pkg.views.get_all_binaries')
ABX_PKG_GET_BINARY        = getattr(settings, 'ABX_PKG_GET_BINARY', 'abx_pkg.views.get_binary')


if isinstance(ABX_PKG_GET_ALL_BINARIES, str):
    get_all_abx_pkg_binaries = import_string(ABX_PKG_GET_ALL_BINARIES)
elif isinstance(ABX_PKG_GET_ALL_BINARIES, Callable):
    get_all_abx_pkg_binaries = ABX_PKG_GET_ALL_BINARIES
else:
    raise ValueError('ABX_PKG_GET_ALL_BINARIES must be a function or dotted import path to a function')

if isinstance(ABX_PKG_GET_BINARY, str):
    get_abx_pkg_binary = import_string(ABX_PKG_GET_BINARY)
elif isinstance(ABX_PKG_GET_BINARY, Callable):
    get_abx_pkg_binary = ABX_PKG_GET_BINARY
else:
    raise ValueError('ABX_PKG_GET_BINARY must be a function or dotted import path to a function')


