__package__ = "abxpkg"

import logging as py_logging
import os
import sys
import pwd
import json
import inspect
import shutil
import stat
import hashlib
import platform
import subprocess
import functools
import tempfile
from contextvars import ContextVar

from typing import (
    Optional,
    cast,
    final,
    Any,
    Literal,
    Protocol,
    runtime_checkable,
    TypeVar,
)
from collections.abc import Callable, Iterable, Mapping

from typing_extensions import TypedDict
from typing import Self
from pathlib import Path

from pydantic_core import ValidationError
from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    validate_call,
    ConfigDict,
    InstanceOf,
    computed_field,
    model_validator,
)

from .semver import SemVer
from .base_types import (
    DEFAULT_LIB_DIR,
    BinName,
    BinDirPath,
    HostBinPath,
    BinProviderName,
    PATHStr,
    InstallArgs,
    Sha256,
    MTimeNs,
    EUID,
    SelfMethodName,
    UNKNOWN_SHA256,
    UNKNOWN_MTIME,
    UNKNOWN_EUID,
    bin_name,
    path_is_executable,
    path_is_script,
    abxpkg_install_root_default,
    bin_abspath,
    bin_abspaths,
    func_takes_args_or_kwargs,
)
from .logging import (
    TRACE_DEPTH,
    format_command,
    format_loaded_binary,
    format_subprocess_output,
    get_logger,
    log_with_trace_depth,
    log_subprocess_output,
    log_method_call,
    summarize_value,
)
from .exceptions import (
    BinProviderInstallError,
    BinProviderUnavailableError,
    BinProviderUninstallError,
    BinProviderUpdateError,
)
from .config import (
    apply_exec_env,
    build_exec_env,
    load_derived_cache,
    save_derived_cache,
)

logger = get_logger(__name__)