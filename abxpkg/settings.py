from typing import TYPE_CHECKING
from collections.abc import Callable

from django.conf import settings
from django.utils.module_loading import import_string

if TYPE_CHECKING:
    from .binary import Binary


ABXPKG_GET_ALL_BINARIES = getattr(
    settings,
    "ABXPKG_GET_ALL_BINARIES",
    "abxpkg.views.get_all_binaries",
)
ABXPKG_GET_BINARY = getattr(settings, "ABXPKG_GET_BINARY", "abxpkg.views.get_binary")


if isinstance(ABXPKG_GET_ALL_BINARIES, str):
    get_all_abxpkg_binaries = import_string(ABXPKG_GET_ALL_BINARIES)
elif isinstance(ABXPKG_GET_ALL_BINARIES, Callable):
    get_all_abxpkg_binaries = ABXPKG_GET_ALL_BINARIES
else:
    raise ValueError(
        "ABXPKG_GET_ALL_BINARIES must be a function or dotted import path to a function",
    )

if isinstance(ABXPKG_GET_BINARY, str):
    get_abxpkg_binary = import_string(ABXPKG_GET_BINARY)
elif isinstance(ABXPKG_GET_BINARY, Callable):
    get_abxpkg_binary = ABXPKG_GET_BINARY
else:
    raise ValueError(
        "ABXPKG_GET_BINARY must be a function or dotted import path to a function",
    )

get_all_abxpkg_binaries: "Callable[[], list[Binary]]"
get_abxpkg_binary: "Callable[[str], Binary | None]"
