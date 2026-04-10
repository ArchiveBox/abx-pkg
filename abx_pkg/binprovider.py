__package__ = "abx_pkg"

import logging as py_logging
import os
import sys
import pwd
import inspect
import shutil
import stat
import hashlib
import platform
import subprocess
import functools
import tempfile
from contextvars import ContextVar
from types import SimpleNamespace

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
    BinName,
    BinDirPath,
    HostBinPath,
    BinProviderName,
    PATHStr,
    InstallArgs,
    Sha256,
    SelfMethodName,
    UNKNOWN_SHA256,
    bin_name,
    path_is_executable,
    path_is_script,
    bin_abspath,
    bin_abspaths,
    func_takes_args_or_kwargs,
)
from .logging import (
    format_command,
    format_loaded_binary,
    format_subprocess_output,
    get_logger,
    log_subprocess_output,
    log_method_call,
)
from .metadata_cache import metadata_cache
from .exceptions import (
    BinProviderInstallError,
    BinProviderUnavailableError,
    BinProviderUninstallError,
    BinProviderUpdateError,
)

logger = get_logger(__name__)

################## GLOBALS ##########################################

OPERATING_SYSTEM = platform.system().lower()
DEFAULT_PATH = "/home/linuxbrew/.linuxbrew/bin:/opt/homebrew/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_ENV_PATH = os.environ.get("PATH", DEFAULT_PATH)
PYTHON_BIN_DIR = str(Path(sys.executable).parent)

if PYTHON_BIN_DIR not in DEFAULT_ENV_PATH:
    DEFAULT_ENV_PATH = PYTHON_BIN_DIR + ":" + DEFAULT_ENV_PATH

UNKNOWN_ABSPATH = Path("/usr/bin/true")
UNKNOWN_VERSION = cast(SemVer, SemVer.parse("999.999.999"))
ACTIVE_EXEC_LOG_PREFIX: ContextVar[str | None] = ContextVar(
    "abx_pkg_active_exec_log_prefix",
    default=None,
)


def env_flag_is_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


################## SUPPLY-CHAIN SECURITY HELPERS ######################


################## VALIDATORS #######################################

NEVER_CACHE = (
    None,
    UNKNOWN_ABSPATH,
    UNKNOWN_VERSION,
    UNKNOWN_SHA256,
)


def binprovider_cache(binprovider_method):
    """cache non-null return values for BinProvider methods on the BinProvider instance"""

    method_name = binprovider_method.__name__

    @functools.wraps(binprovider_method)
    def cached_function(self, bin_name: BinName, **kwargs):
        self._cache = self._cache or {}
        self._cache[method_name] = self._cache.get(method_name, {})
        method_cache = self._cache[method_name]

        if bin_name in method_cache and not kwargs.get("no_cache"):
            # print('USING CACHED VALUE:', f'{self.__class__.__name__}.{method_name}({bin_name}, {kwargs}) -> {method_cache[bin_name]}')
            return method_cache[bin_name]

        return_value = binprovider_method(self, bin_name, **kwargs)

        if return_value and return_value not in NEVER_CACHE:
            self._cache[method_name][bin_name] = return_value
        return return_value

    cached_function.__name__ = f"{method_name}_cached"

    return cached_function


R = TypeVar("R")


def remap_kwargs(
    renamed_kwargs: Mapping[str, str],
) -> Callable[[Callable[..., R]], Callable[..., R]]:
    def decorator(func: Callable[..., R]) -> Callable[..., R]:
        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> R:
            mapped_kwargs = dict(kwargs)
            for old_name, new_name in renamed_kwargs.items():
                if old_name in mapped_kwargs:
                    mapped_kwargs.setdefault(new_name, mapped_kwargs[old_name])
                    mapped_kwargs.pop(old_name, None)
            return func(*args, **mapped_kwargs)

        return wrapper

    return decorator


class ShallowBinary(BaseModel):
    """
    Shallow version of Binary used as a return type for BinProvider methods (e.g. install()).
    (doesn't implement full Binary interface, but can be used to populate a full loaded Binary instance)
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_by_alias=True,
        validate_by_name=True,
        validate_default=True,
        validate_assignment=False,
        from_attributes=True,
        arbitrary_types_allowed=True,
    )

    name: BinName = ""
    description: str = ""

    binproviders: list[InstanceOf["BinProvider"]] = Field(default_factory=list)
    overrides: "BinaryOverrides" = Field(default_factory=dict)

    loaded_binprovider: InstanceOf["BinProvider"] | None = Field(
        default=None,
        alias="binprovider",
    )
    loaded_abspath: HostBinPath | None = Field(default=None, alias="abspath")
    loaded_version: SemVer | None = Field(default=None, alias="version")
    loaded_sha256: Sha256 | None = Field(default=None, alias="sha256")

    def __getattr__(self, item: str) -> Any:
        """Allow accessing loaded fields by their alias names."""
        for field, meta in type(self).model_fields.items():
            if meta.alias == item:
                return getattr(self, field)
        raise AttributeError(item)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"name={self.name!r}, "
            f"abspath={self.loaded_abspath!r}, "
            f"version={self.loaded_version!r}, "
            f"sha256={f'...{str(self.loaded_sha256)[-6:]}' if self.loaded_sha256 else None!r}"
            f")"
        )

    __str__ = __repr__

    @model_validator(mode="after")
    def validate_model(self) -> Self:
        self.description = self.description or self.name
        return self

    @computed_field  # see mypy issue #1362
    @property
    def bin_filename(self) -> BinName:
        if self.is_script:
            # e.g. '.../Python.framework/Versions/3.11/lib/python3.11/sqlite3/__init__.py' -> sqlite
            name = self.name
        elif self.loaded_abspath:
            # e.g. '/opt/homebrew/bin/wget' -> wget
            name = bin_name(self.loaded_abspath)
        else:
            # e.g. 'ytdlp' -> 'yt-dlp'
            name = bin_name(self.name)
        return name

    @computed_field  # see mypy issue #1362
    @property
    def is_executable(self) -> bool:
        try:
            assert self.loaded_abspath and path_is_executable(self.loaded_abspath)
            return True
        except (ValidationError, AssertionError):
            return False

    @computed_field  # see mypy issue #1362
    @property
    def is_script(self) -> bool:
        try:
            assert self.loaded_abspath and path_is_script(self.loaded_abspath)
            return True
        except (ValidationError, AssertionError):
            return False

    @computed_field  # see mypy issue #1362
    @property
    def is_valid(self) -> bool:
        return bool(
            self.name and self.loaded_abspath and self.loaded_version,
        )

    @computed_field
    @property
    def bin_dir(self) -> BinDirPath | None:
        if not self.loaded_abspath:
            return None
        try:
            return TypeAdapter(BinDirPath).validate_python(self.loaded_abspath.parent)
        except (ValidationError, AssertionError):
            return None

    @computed_field
    @property
    def loaded_respath(self) -> HostBinPath | None:
        return self.loaded_abspath and self.loaded_abspath.resolve()

    # @validate_call
    @log_method_call(include_result=True)
    def exec(
        self,
        bin_name: BinName | HostBinPath | None = None,
        cmd: Iterable[str | Path | int | float | bool] = (),
        cwd: str | Path = ".",
        quiet=False,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        bin_name = str(bin_name or self.loaded_abspath or self.name)
        if bin_name == self.name:
            assert self.loaded_abspath, (
                "Binary must have a loaded_abspath, make sure to load() or install() first"
            )
            assert self.loaded_version, (
                "Binary must have a loaded_version, make sure to load() or install() first"
            )
        assert os.path.isdir(cwd) and os.access(cwd, os.R_OK), (
            f"cwd must be a valid, accessible directory: {cwd}"
        )
        cmd = [str(bin_name), *(str(arg) for arg in cmd)]
        logger.debug("Executing binary command: %s", format_command(cmd))
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            **kwargs,
        )


class BinProvider(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_by_alias=True,
        validate_by_name=True,
        validate_default=True,
        validate_assignment=False,
        from_attributes=True,
        revalidate_instances="always",
        arbitrary_types_allowed=True,
    )
    name: BinProviderName = ""

    PATH: PATHStr = Field(
        default=str(Path(sys.executable).parent),
        repr=False,
    )  # e.g.  '/opt/homebrew/bin:/opt/archivebox/bin'
    INSTALLER_BIN: BinName = "env"

    euid: int | None = None
    install_root: Path | None = None
    bin_dir: Path | None = None
    dry_run: bool = Field(
        default_factory=lambda: (
            env_flag_is_true("ABX_PKG_DRY_RUN")
            if "ABX_PKG_DRY_RUN" in os.environ
            else env_flag_is_true("DRY_RUN")
        ),
    )
    postinstall_scripts: bool | None = Field(default=None)
    min_release_age: float | None = Field(default=None)

    overrides: "BinProviderOverrides" = Field(  # ty: ignore[invalid-assignment] https://github.com/astral-sh/ty/issues/2403
        default_factory=lambda: {
            "*": {
                "version": "self.default_version_handler",
                "abspath": "self.default_abspath_handler",
                "install_args": "self.default_install_args_handler",
                "install": "self.default_install_handler",
                "update": "self.default_update_handler",
                "uninstall": "self.default_uninstall_handler",
            },
        },
        repr=False,
        exclude=True,
    )

    install_timeout: int = Field(
        default_factory=lambda: int(os.environ.get("ABX_PKG_INSTALL_TIMEOUT", "120")),
        repr=False,
    )
    version_timeout: int = Field(
        default_factory=lambda: int(os.environ.get("ABX_PKG_VERSION_TIMEOUT", "10")),
        repr=False,
    )
    _cache: dict[str, dict[str, Any]] | None = None
    _INSTALLER_BIN_ABSPATH: HostBinPath | None = (
        None  # speed optimization only, faster to cache the abspath than to recompute it on every access
    )
    _INSTALLER_BINARY: ShallowBinary | None = (
        None  # speed optimization only, faster to cache the binary than to recompute it on every access
    )

    def __eq__(self, other: Any) -> bool:
        try:
            return (
                dict(self) == dict(other)
            )  # only compare pydantic fields, ignores classvars/@properties/@cached_properties/_fields/etc.
        except Exception:
            return False

    @staticmethod
    def uid_has_passwd_entry(uid: int) -> bool:
        try:
            pwd.getpwuid(uid)
        except KeyError:
            return False
        return True

    def detect_euid(
        self,
        owner_paths: Iterable[str | Path | None] = (),
        preserve_root: bool = False,
    ) -> int:
        current_euid = os.geteuid()
        candidate_euid = None

        for path in owner_paths:
            if path and os.path.isdir(path):
                candidate_euid = os.stat(path).st_uid
                break

        if candidate_euid is None:
            if preserve_root and current_euid == 0:
                candidate_euid = 0
            else:
                try:
                    installer_bin = self.INSTALLER_BIN_ABSPATH
                    if installer_bin:
                        installer_owner = os.stat(installer_bin).st_uid
                        if installer_owner != 0:
                            candidate_euid = installer_owner
                except Exception:
                    # INSTALLER_BIN_ABSPATH is not always available (e.g. at import time, or if it dynamically changes)
                    pass

        if candidate_euid is not None and not self.uid_has_passwd_entry(candidate_euid):
            candidate_euid = current_euid

        return candidate_euid if candidate_euid is not None else current_euid

    def get_pw_record(self, uid: int) -> Any:
        try:
            return pwd.getpwuid(uid)
        except KeyError:
            if uid != os.geteuid():
                raise
            return SimpleNamespace(
                pw_uid=uid,
                pw_gid=os.getegid(),
                pw_dir=os.environ.get("HOME", tempfile.gettempdir()),
                pw_name=os.environ.get("USER") or os.environ.get("LOGNAME") or str(uid),
            )

    @property
    def EUID(self) -> int:
        """
        Detect the user (UID) to run as when executing this binprovider's INSTALLER_BIN
        e.g. homebrew should never be run as root, we can tell which user to run it as by looking at who owns its binary
        apt should always be run as root, pip should be run as the user that owns the venv, etc.
        """

        # use user-provided value if one is set
        if self.euid is not None:
            return self.euid

        return self.detect_euid()

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Actual absolute path of the underlying package manager (e.g. /usr/local/bin/npm)"""
        if self._INSTALLER_BIN_ABSPATH:
            # return cached value if we have one
            return self._INSTALLER_BIN_ABSPATH

        abspath = bin_abspath(self.INSTALLER_BIN, PATH=self.PATH) or bin_abspath(
            self.INSTALLER_BIN,
        )  # find self.INSTALLER_BIN abspath using environment path
        if not abspath:
            # underlying package manager not found on this host, return None
            return None

        valid_abspath = TypeAdapter(HostBinPath).validate_python(abspath)
        if valid_abspath:
            # if we found a valid abspath, cache it
            self._INSTALLER_BIN_ABSPATH = valid_abspath
        return valid_abspath

    @property
    def INSTALLER_BINARY(self) -> ShallowBinary | None:
        """Get the loaded binary for this binprovider's INSTALLER_BIN"""

        if self._INSTALLER_BINARY:
            # return cached value if we have one
            return self._INSTALLER_BINARY

        abspath = self.INSTALLER_BIN_ABSPATH
        if not abspath:
            return None

        try:
            # try loading it from the BinProvider's own PATH (e.g. ~/test/.venv/bin/pip)
            loaded_bin = self.load(bin_name=self.INSTALLER_BIN)
            if loaded_bin:
                self._INSTALLER_BINARY = loaded_bin
                return loaded_bin
        except Exception:
            pass

        env = EnvProvider()
        try:
            # try loading it from the env provider (e.g. /opt/homebrew/bin/pip)
            loaded_bin = env.load(bin_name=self.INSTALLER_BIN)
            if loaded_bin:
                self._INSTALLER_BINARY = loaded_bin
                return loaded_bin
        except Exception:
            pass

        version = UNKNOWN_VERSION
        sha256 = UNKNOWN_SHA256

        return ShallowBinary.model_validate(
            {
                "name": self.INSTALLER_BIN,
                "abspath": abspath,
                "binprovider": env,
                "version": version,
                "sha256": sha256,
            },
        )

    @computed_field
    @property
    def is_valid(self) -> bool:
        return bool(self.INSTALLER_BIN_ABSPATH)

    @final
    # @validate_call(config={'arbitrary_types_allowed': True})
    @log_method_call()
    def get_provider_with_overrides(
        self,
        overrides: Optional["BinProviderOverrides"] = None,
        dry_run: bool | None = None,
        install_timeout: int | None = None,
        version_timeout: int | None = None,
    ) -> Self:
        # created an updated copy of the BinProvider with the overrides applied, then get the handlers on it.
        # important to do this so that any subsequent calls to handler functions down the call chain
        # still have access to the overrides, we don't have to have to pass them down as args all the way down the stack

        updated_binprovider: Self = self.model_copy(deep=True)

        # main binary-specific overrides for [abspath, version, install_args, install, update, uninstall]
        overrides = overrides or {}

        # extra overrides that are also configurable, can add more in the future as-needed for tunable options
        updated_binprovider.dry_run = self.dry_run if dry_run is None else dry_run
        updated_binprovider.install_timeout = (
            self.install_timeout if install_timeout is None else install_timeout
        )
        updated_binprovider.version_timeout = (
            self.version_timeout if version_timeout is None else version_timeout
        )

        # overrides = {
        #     'wget': {
        #         'install_args': lambda: ['wget'],
        #         'abspath': lambda: shutil.which('wget'),
        #         'version': lambda: SemVer.parse(os.system('wget --version')),
        #         'install': lambda: os.system('brew install wget'),
        #     },
        # }
        for binname, bin_overrides in overrides.items():
            updated_binprovider.overrides[binname] = {
                **updated_binprovider.overrides.get(binname, {}),
                **bin_overrides,
            }

        return updated_binprovider

    # @validate_call
    @log_method_call(include_result=True)
    def _get_handler_keys(
        self,
        handler_type: "HandlerType",
    ) -> tuple["HandlerType", ...]:
        if handler_type in ("install_args", "packages"):
            return ("install_args", "packages")
        return (handler_type,)

    @log_method_call(include_result=True)
    def _get_handler_for_action(
        self,
        bin_name: BinName,
        handler_type: "HandlerType",
    ) -> Callable[..., "HandlerReturnValue"]:
        """
        Get the handler func for a given key + Dict of handler callbacks + fallback default handler.
        e.g. _get_handler_for_action(bin_name='yt-dlp', 'install', default_handler=self.default_install_handler, ...) -> Callable
        """

        handler: HandlerValue | None = None
        for overrides_for_bin in (
            self.overrides.get(bin_name, {}),
            self.overrides.get("*", {}),
        ):
            for handler_key in self._get_handler_keys(handler_type):
                handler = overrides_for_bin.get(handler_key)
                if handler:
                    break
            if handler:
                break
        # print('getting handler for action', bin_name, handler_type, handler_func)
        assert handler, (
            f"🚫 BinProvider(name={self.name}) has no {handler_type} handler implemented for Binary(name={bin_name})"
        )

        # if handler_func is already a callable, return it directly
        if isinstance(handler, Callable):
            return handler

        # if handler_func is string reference to a function on self, swap it for the actual function
        elif isinstance(handler, str) and (
            handler.startswith("self.") or handler.startswith("BinProvider.")
        ):
            # special case, allow dotted path references to methods on self (where self refers to the BinProvider)
            handler_method: Callable[..., HandlerReturnValue] = getattr(
                self,
                handler.split("self.", 1)[-1],
            )
            return handler_method

        # if handler_func is any other value, treat is as a literal and return a func that provides the literal
        literal_value = TypeAdapter(HandlerReturnValue).validate_python(handler)

        def literal_handler() -> HandlerReturnValue:
            return literal_value

        return literal_handler

    # @validate_call
    @log_method_call(include_result=True)
    def _get_compatible_kwargs(
        self,
        handler_func: Callable[..., "HandlerReturnValue"],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        if not kwargs:
            return kwargs

        signature = inspect.signature(handler_func)
        if any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        ):
            return kwargs

        accepted_kwargs = set(signature.parameters)
        return {key: value for key, value in kwargs.items() if key in accepted_kwargs}

    @log_method_call(include_result=True)
    def _call_handler_for_action(
        self,
        bin_name: BinName,
        handler_type: "HandlerType",
        **kwargs,
    ) -> "HandlerReturnValue":
        handler_func: Callable[..., HandlerReturnValue] = self._get_handler_for_action(
            bin_name=bin_name,  # e.g. 'yt-dlp', or 'wget', etc.
            handler_type=handler_type,  # e.g. abspath, version, install_args, install
        )

        # def timeout_handler(signum, frame):
        # raise TimeoutError(f'{self.__class__.__name__} Timeout while running {handler_type} for Binary {bin_name}')

        # signal ONLY WORKS IN MAIN THREAD, not a viable solution for timeout enforcement! breaks in prod
        # signal.signal(signal.SIGALRM, handler=timeout_handler)
        # signal.alarm(timeout)
        try:
            if not func_takes_args_or_kwargs(handler_func):
                # if it's a pure argless lambda/func, dont pass bin_path and other **kwargs
                handler_func_without_args = cast(
                    Callable[[], HandlerReturnValue],
                    handler_func,
                )
                return handler_func_without_args()

            compatible_kwargs = self._get_compatible_kwargs(handler_func, kwargs)
            if hasattr(handler_func, "__self__"):
                # func is already a method bound to self, just call it directly
                return handler_func(bin_name, **compatible_kwargs)
            else:
                # func is not bound to anything, pass BinProvider as first arg
                return handler_func(self, bin_name, **compatible_kwargs)
        except TimeoutError:
            raise
        # finally:
        #     signal.alarm(0)

    # DEFAULT HANDLERS, override these in subclasses as needed:

    # @validate_call
    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        **context,
    ) -> "AbspathFuncReturnValue":  # aka str | Path | None
        # print(f'[*] {self.__class__.__name__}: Getting abspath for {bin_name}...')

        if not self.PATH:
            return None

        bin_dir = getattr(self, "bin_dir", None)
        if bin_dir is not None:
            managed_abspath = bin_abspath(bin_name, PATH=str(bin_dir))
            if managed_abspath is not None:
                return managed_abspath
            return None

        return bin_abspath(bin_name, PATH=self.PATH)

    # @validate_call
    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        **context,
    ) -> "VersionFuncReturnValue":  # aka List[str] | Tuple[str, ...]
        return self._version_from_exec(
            bin_name,
            abspath=abspath,
            timeout=timeout,
        )

    # @validate_call
    def default_install_args_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> "InstallArgsFuncReturnValue":  # aka List[str] aka InstallArgs
        # print(f'[*] {self.__class__.__name__}: Getting install command for {bin_name}')
        # ... install command calculation logic here
        return [bin_name]

    def default_packages_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> "InstallArgsFuncReturnValue":
        return self.default_install_args_handler(bin_name, **context)

    # @validate_call
    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> "InstallFuncReturnValue":  # aka str
        self.setup(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
            no_cache=no_cache,
        )
        install_args = install_args or self.get_install_args(bin_name)
        self._require_installer_bin()

        # print(f'[*] {self.__class__.__name__}: Installing {bin_name}: {self.INSTALLER_BIN_ABSPATH} {install_args}')

        # ... override the default install logic here ...

        # proc = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=['install', *install_args], timeout=self.install_timeout)
        # if not proc.returncode == 0:
        #     print(proc.stdout.strip())
        #     print(proc.stderr.strip())
        #     raise Exception(f'{self.name} Failed to install {bin_name}: {proc.stderr.strip()}\n{proc.stdout.strip()}')

        return f"🚫 {self.name} BinProvider does not implement any .install() method"

    # @validate_call
    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> "ActionFuncReturnValue":
        self._require_installer_bin()
        return f"🚫 {self.name} BinProvider does not implement any .update() method"

    # @validate_call
    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> "ActionFuncReturnValue":
        self._require_installer_bin()
        return False

    @log_method_call()
    def invalidate_cache(self, bin_name: BinName) -> None:
        if self._cache:
            for method_cache in self._cache.values():
                method_cache.pop(bin_name, None)
        metadata_cache.invalidate(self.name, bin_name, self.install_root)

    @log_method_call()
    def setup_PATH(self) -> None:
        for path in reversed(self.PATH.split(":")):
            if path not in sys.path:
                sys.path.insert(
                    0,
                    path,
                )  # e.g. /opt/archivebox/bin:/bin:/usr/local/bin:...

    def _require_installer_bin(self) -> HostBinPath:
        installer_bin = self.INSTALLER_BIN_ABSPATH
        if installer_bin:
            return installer_bin
        raise BinProviderUnavailableError(
            self.__class__.__name__,
            self.INSTALLER_BIN,
        )

    def _merge_PATH(
        self,
        *entries: str | Path,
        PATH: str | None = None,
        prepend: bool = False,
    ) -> PATHStr:
        new_entries = [str(entry) for entry in entries if str(entry)]
        existing_entries = [entry for entry in (PATH or "").split(":") if entry]
        merged_entries = (
            [*new_entries, *existing_entries]
            if prepend
            else [*existing_entries, *new_entries]
        )
        return TypeAdapter(PATHStr).validate_python(
            ":".join(dict.fromkeys(merged_entries)),
        )

    def _version_from_exec(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
    ) -> SemVer | None:
        abspath = abspath or self.get_abspath(bin_name, quiet=True)
        if not abspath:
            return None

        timeout = self.version_timeout if timeout is None else timeout
        validation_err = None
        version_outputs: list[str] = []

        for version_arg in ("--version", "-version", "-v"):
            proc = self.exec(
                bin_name=abspath,
                cmd=[version_arg],
                timeout=timeout,
                quiet=True,
            )
            version_output = proc.stdout.strip() or proc.stderr.strip()
            version_outputs.append(version_output)
            if proc.returncode != 0:
                validation_err = validation_err or AssertionError(
                    f"❌ $ {bin_name} {version_arg} exited with status {proc.returncode}",
                )
                continue
            try:
                version = SemVer.parse(version_output)
                assert version, (
                    f"❌ Could not parse version from $ {bin_name} {version_arg}: {version_output}".strip()
                )
                return version
            except (ValidationError, AssertionError) as err:
                validation_err = validation_err or err

        raise ValueError(
            f"❌ Unable to find {bin_name} version from {bin_name} --version, -version or -v output\n{next((output for output in version_outputs if output), '')}".strip(),
        ) from validation_err

    def _ensure_writable_cache_dir(self, cache_dir: Path) -> bool:
        if cache_dir.exists() and not cache_dir.is_dir():
            return False

        cache_dir.mkdir(parents=True, exist_ok=True)

        pw_record = self.get_pw_record(self.EUID)
        try:
            os.chown(cache_dir, self.EUID, pw_record.pw_gid)
        except PermissionError:
            pass

        try:
            cache_dir.chmod(
                cache_dir.stat().st_mode | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH,
            )
        except PermissionError:
            pass

        return cache_dir.is_dir() and os.access(cache_dir, os.W_OK)

    def _raise_proc_error(
        self,
        action: Literal["install", "update", "uninstall"],
        target: object,
        proc: subprocess.CompletedProcess,
    ) -> None:
        log_subprocess_output(
            logger,
            f"{self.__class__.__name__} {action}",
            proc.stdout,
            proc.stderr,
            level=py_logging.ERROR,
        )
        exc_cls = {
            "install": BinProviderInstallError,
            "update": BinProviderUpdateError,
            "uninstall": BinProviderUninstallError,
        }[action]
        raise exc_cls(
            self.__class__.__name__,
            target,
            returncode=proc.returncode,
            output=format_subprocess_output(proc.stdout, proc.stderr),
        )

    # @validate_call
    @log_method_call(include_result=True)
    def exec(
        self,
        bin_name: BinName | HostBinPath,
        cmd: Iterable[str | Path | int | float | bool] = (),
        cwd: Path | str = ".",
        quiet=False,
        should_log_command: bool = True,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        explicit_abspath = Path(str(bin_name)).expanduser()
        if (
            explicit_abspath.is_absolute()
            and explicit_abspath.is_file()
            and os.access(explicit_abspath, os.X_OK)
        ):
            bin_abspath = explicit_abspath
        else:
            bin_abspath = self.get_abspath(str(bin_name)) or shutil.which(str(bin_name))
        assert bin_abspath, (
            f"❌ BinProvider {self.name} cannot execute bin_name {bin_name} because it could not find its abspath. (Did {self.__class__.__name__}.install({bin_name}) fail?)"
        )
        assert os.access(cwd, os.R_OK) and os.path.isdir(cwd), (
            f"cwd must be a valid, accessible directory: {cwd}"
        )
        cwd_path = Path(cwd).resolve()
        cmd = [str(bin_abspath), *(str(arg) for arg in cmd)]
        exec_log_prefix = ACTIVE_EXEC_LOG_PREFIX.get()
        if should_log_command:
            if exec_log_prefix:
                logger.info("$ %s", format_command(cmd))
            elif self.dry_run:
                logger.info(
                    "DRY RUN (%s): %s",
                    self.__class__.__name__,
                    format_command(cmd),
                )

        # https://stackoverflow.com/a/6037494/2156113
        # copy env and modify it to run the subprocess as the the designated user
        current_euid = os.geteuid()
        explicit_env = kwargs.pop("env", None)
        base_env = (explicit_env or os.environ.copy()).copy()
        base_env["PATH"] = self._merge_PATH(
            *base_env.get("PATH", "").split(":"),
            PATH=self.PATH,
        )
        base_env["PWD"] = str(cwd_path)
        target_pw_record = self.get_pw_record(self.EUID)
        current_pw_record = self.get_pw_record(current_euid)
        run_as_uid = target_pw_record.pw_uid
        run_as_gid = target_pw_record.pw_gid

        def _env_for_identity(
            identity: Any,
            *,
            source_env: dict[str, str],
        ) -> dict[str, str]:
            env = source_env.copy()
            env["HOME"] = identity.pw_dir
            env["LOGNAME"] = identity.pw_name
            env["USER"] = identity.pw_name
            return env

        sudo_env = _env_for_identity(target_pw_record, source_env=base_env)
        fallback_env = _env_for_identity(current_pw_record, source_env=base_env)

        def drop_privileges():
            try:
                os.setuid(run_as_uid)
                os.setgid(run_as_gid)
            except Exception:
                pass

        if self.dry_run:
            return subprocess.CompletedProcess(cmd, 0, "", "skipped (dry run)")

        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)

        sudo_failure_output = None
        if current_euid != 0 and run_as_uid != current_euid:
            sudo_abspath = shutil.which("sudo", path=sudo_env["PATH"]) or shutil.which(
                "sudo",
            )
            if sudo_abspath:
                sudo_cmd = [sudo_abspath, "-n"]
                if run_as_uid != 0:
                    sudo_cmd.extend(["-u", target_pw_record.pw_name])
                sudo_cmd.extend(["--", *cmd])
                sudo_proc = subprocess.run(
                    sudo_cmd,
                    cwd=str(cwd_path),
                    env=sudo_env,
                    **kwargs,
                )
                if sudo_proc.returncode == 0:
                    return sudo_proc
                log_subprocess_output(
                    logger,
                    f"{self.__class__.__name__} sudo exec",
                    sudo_proc.stdout,
                    sudo_proc.stderr,
                    level=py_logging.DEBUG,
                )
                sudo_failure_output = format_subprocess_output(
                    sudo_proc.stdout,
                    sudo_proc.stderr,
                )

        proc = subprocess.run(
            cmd,
            cwd=str(cwd_path),
            env=fallback_env,
            preexec_fn=drop_privileges,
            **kwargs,
        )
        if sudo_failure_output and proc.returncode != 0:
            return subprocess.CompletedProcess(
                proc.args,
                proc.returncode,
                proc.stdout,
                "\n".join(
                    part
                    for part in (
                        proc.stderr,
                        f"Previous sudo attempt failed:\n{sudo_failure_output}",
                    )
                    if part
                ),
            )
        return proc

    # CALLING API, DONT OVERRIDE THESE:

    @final
    @binprovider_cache
    # @validate_call
    @log_method_call(include_result=True)
    def get_abspaths(
        self,
        bin_name: BinName,
        no_cache: bool = False,
    ) -> list[HostBinPath]:
        abspaths: list[HostBinPath] = []

        primary_abspath = self.get_abspath(bin_name, quiet=True, no_cache=no_cache)
        if primary_abspath:
            abspaths.append(primary_abspath)

        for abspath in bin_abspaths(bin_name, PATH=self.PATH):
            if abspath not in abspaths:
                abspaths.append(abspath)

        return abspaths

    @final
    @binprovider_cache
    # @validate_call
    @log_method_call(include_result=True)
    def get_sha256(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        no_cache: bool = False,
    ) -> Sha256 | None:
        """Get the sha256 hash of the binary at the given abspath (or equivalent hash of the underlying package)"""

        abspath = abspath or self.get_abspath(bin_name, no_cache=no_cache)
        if not abspath or not os.access(abspath, os.R_OK):
            return None

        hash_sha256 = hashlib.sha256()
        with open(abspath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        return TypeAdapter(Sha256).validate_python(hash_sha256.hexdigest())

    @final
    @binprovider_cache
    # @validate_call
    @log_method_call(include_result=True)
    def get_abspath(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
    ) -> HostBinPath | None:
        self.setup_PATH()
        abspath = None
        try:
            abspath = cast(
                AbspathFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="abspath",
                ),
            )
        except Exception:
            # logger.warning(
            #     "Provider %s failed to resolve abspath for %s: %s",
            #     self.name,
            #     bin_name,
            #     err,
            # )
            if not quiet:
                raise
        if not abspath:
            return None
        result = TypeAdapter(HostBinPath).validate_python(abspath)
        return result

    @final
    @binprovider_cache
    # @validate_call
    @log_method_call(include_result=True)
    def get_version(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        quiet: bool = False,
        no_cache: bool = False,
    ) -> SemVer | None:
        version = None
        try:
            version = cast(
                VersionFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="version",
                    abspath=abspath,
                    timeout=self.version_timeout,
                ),
            )
        except Exception as err:
            logger.warning(
                "%s failed to resolve version for %s: %s",
                self.name,
                bin_name,
                err,
            )
            if not quiet:
                raise

        if not version:
            return None

        if not isinstance(version, SemVer):
            version = SemVer.parse(version)

        return version

    @final
    @binprovider_cache
    # @validate_call
    @log_method_call(include_result=True)
    def get_install_args(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
    ) -> InstallArgs:
        install_args = None
        try:
            install_args = cast(
                InstallArgsFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="install_args",
                ),
            )
        except Exception:
            # logger.warning(
            #     "Provider %s failed to resolve install args for %s: %s",
            #     self.name,
            #     bin_name,
            #     err,
            # )
            if not quiet:
                raise

        if not install_args:
            install_args = [bin_name]
        result = TypeAdapter(InstallArgs).validate_python(install_args)
        return result

    @log_method_call(include_result=True)
    def get_packages(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
    ) -> InstallArgs:
        return self.get_install_args(bin_name, quiet=quiet, no_cache=no_cache)

    @log_method_call()
    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        """Override this to do any setup steps needed before installing packaged (e.g. create a venv, init an npm prefix, etc.)"""
        pass

    def supports_min_release_age(self, action: Literal["install", "update"]) -> bool:
        return False

    def supports_postinstall_disable(
        self,
        action: Literal["install", "update"],
    ) -> bool:
        return False

    def _assert_min_version_satisfied(
        self,
        *,
        bin_name: BinName,
        action: Literal["install", "update"],
        loaded_version: SemVer | None,
        min_version: SemVer | None,
    ) -> None:
        if min_version and loaded_version and loaded_version < min_version:
            raise ValueError(
                f"🚫 {self.__class__.__name__}.{action} resolved {bin_name} with version {loaded_version} which does not satisfy min_version {min_version}",
            )

    @final
    @log_method_call(include_result=True)
    @validate_call
    def install(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
        dry_run: bool | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> ShallowBinary | None:
        if dry_run is not None and dry_run != self.dry_run:
            return self.get_provider_with_overrides(dry_run=dry_run).install(
                bin_name=bin_name,
                quiet=quiet,
                no_cache=no_cache,
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
                min_version=min_version,
            )
        postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        if not no_cache:
            try:
                installed = self.load(bin_name=bin_name, quiet=True, no_cache=False)
            except Exception:
                installed = None
            if (
                installed is not None
                and min_version is not None
                and installed.loaded_version is not None
                and installed.loaded_version < min_version
            ):
                installed = self.update(
                    bin_name=bin_name,
                    quiet=quiet,
                    no_cache=False,
                    dry_run=dry_run,
                    postinstall_scripts=postinstall_scripts,
                    min_release_age=min_release_age,
                    min_version=min_version,
                )
            if installed:
                return installed

        install_args = self.get_install_args(bin_name, quiet=quiet, no_cache=no_cache)
        if (
            min_release_age is not None
            and min_release_age > 0
            and not self.supports_min_release_age("install")
        ):
            logger.warning(
                "⚠️ %s.install ignoring unsupported min_release_age=%s for %s",
                self.__class__.__name__,
                min_release_age,
                self.name,
            )
            min_release_age = None
        if postinstall_scripts is False and not self.supports_postinstall_disable(
            "install",
        ):
            logger.warning(
                "⚠️ %s.install ignoring unsupported postinstall_scripts=%s for %s",
                self.__class__.__name__,
                postinstall_scripts,
                self.name,
            )
            postinstall_scripts = None
        self.setup(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
            no_cache=no_cache,
        )

        self.setup_PATH()
        install_log = None
        exec_log_prefix_token = ACTIVE_EXEC_LOG_PREFIX.set(
            f"⛟  Installing {bin_name} via {self.name}...",
        )
        logger.info(ACTIVE_EXEC_LOG_PREFIX.get())
        try:
            install_log = cast(
                InstallFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="install",
                    install_args=install_args,
                    packages=install_args,
                    no_cache=no_cache,
                    postinstall_scripts=postinstall_scripts,
                    min_release_age=min_release_age,
                    min_version=min_version,
                    timeout=self.install_timeout,
                ),
            )
        except Exception as err:
            install_log = f"❌ {self.__class__.__name__} Failed to install {bin_name}, got {err.__class__.__name__}: {err}"
            if not quiet:
                raise
        finally:
            ACTIVE_EXEC_LOG_PREFIX.reset(exec_log_prefix_token)

        if self.dry_run:
            # return fake ShallowBinary if we're just doing a dry run
            # no point trying to get real abspath or version if nothing was actually installed
            return ShallowBinary.model_validate(
                {
                    "name": bin_name,
                    "binprovider": self,
                    "abspath": Path(shutil.which(bin_name) or UNKNOWN_ABSPATH),
                    "version": UNKNOWN_VERSION,
                    "sha256": UNKNOWN_SHA256,
                    "binproviders": [self],
                },
            )

        self.invalidate_cache(bin_name)

        installed_abspath = self.get_abspath(bin_name, quiet=True, no_cache=no_cache)
        if not quiet:
            assert installed_abspath, (
                f"❌ {self.__class__.__name__} Unable to find abspath for {bin_name} after installing. PATH={self.PATH} LOG={install_log}"
            )

        installed_version = self.get_version(
            bin_name,
            abspath=installed_abspath,
            quiet=True,
            no_cache=no_cache,
        )
        if not quiet:
            assert installed_version, (
                f"❌ {self.__class__.__name__} Unable to find version for {bin_name} after installing. ABSPATH={installed_abspath} LOG={install_log}"
            )
        self._assert_min_version_satisfied(
            bin_name=bin_name,
            action="install",
            loaded_version=installed_version,
            min_version=min_version,
        )

        sha256 = (
            self.get_sha256(bin_name, abspath=installed_abspath, no_cache=no_cache)
            or UNKNOWN_SHA256
        )

        if installed_abspath and installed_version:
            # installed binary is valid and ready to use
            result = ShallowBinary.model_validate(
                {
                    "name": bin_name,
                    "binprovider": self,
                    "abspath": installed_abspath,
                    "version": installed_version,
                    "sha256": sha256,
                    "binproviders": [self],
                },
            )
            # Persist to on-disk cache for fast loading in future processes
            metadata_cache.set(
                self.name,
                bin_name,
                self.install_root,
                installed_abspath,
                str(installed_version),
                sha256,
            )
            logger.info(
                format_loaded_binary(
                    "🆕 Installed",
                    installed_abspath,
                    installed_version,
                    self,
                ),
                extra={"abx_cli_duplicate_stdout": True},
            )
            return result
        return None

    @final
    @log_method_call(include_result=True)
    @validate_call
    def update(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
        dry_run: bool | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> ShallowBinary | None:
        if dry_run is not None and dry_run != self.dry_run:
            return self.get_provider_with_overrides(dry_run=dry_run).update(
                bin_name=bin_name,
                quiet=quiet,
                no_cache=no_cache,
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
                min_version=min_version,
            )
        postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        install_args = self.get_install_args(bin_name, quiet=quiet, no_cache=no_cache)
        if (
            min_release_age is not None
            and min_release_age > 0
            and not self.supports_min_release_age("update")
        ):
            logger.warning(
                "⚠️ %s.update ignoring unsupported min_release_age=%s for provider %s",
                self.__class__.__name__,
                min_release_age,
                self.name,
            )
            min_release_age = None
        if postinstall_scripts is False and not self.supports_postinstall_disable(
            "update",
        ):
            logger.warning(
                "⚠️ %s.update ignoring unsupported postinstall_scripts=%s for provider %s",
                self.__class__.__name__,
                postinstall_scripts,
                self.name,
            )
            postinstall_scripts = None
        self.setup(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
            no_cache=no_cache,
        )

        self.setup_PATH()
        update_log = None
        exec_log_prefix_token = ACTIVE_EXEC_LOG_PREFIX.set(
            f"⬆ Updating {bin_name} via {self.name}...",
        )
        logger.info(ACTIVE_EXEC_LOG_PREFIX.get())
        try:
            update_log = cast(
                ActionFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="update",
                    install_args=install_args,
                    packages=install_args,
                    no_cache=no_cache,
                    postinstall_scripts=postinstall_scripts,
                    min_release_age=min_release_age,
                    min_version=min_version,
                    timeout=self.install_timeout,
                ),
            )
        except Exception as err:
            update_log = f"❌ {self.__class__.__name__} Failed to update {bin_name}, got {err.__class__.__name__}: {err}"
            if not quiet:
                raise
        finally:
            ACTIVE_EXEC_LOG_PREFIX.reset(exec_log_prefix_token)

        if self.dry_run:
            return ShallowBinary.model_validate(
                {
                    "name": bin_name,
                    "binprovider": self,
                    "abspath": Path(shutil.which(bin_name) or UNKNOWN_ABSPATH),
                    "version": UNKNOWN_VERSION,
                    "sha256": UNKNOWN_SHA256,
                    "binproviders": [self],
                },
            )

        self.invalidate_cache(bin_name)

        updated_abspath = self.get_abspath(bin_name, quiet=True, no_cache=True)
        if not quiet:
            assert updated_abspath, (
                f"❌ {self.__class__.__name__} Unable to find abspath for {bin_name} after updating. PATH={self.PATH} LOG={update_log}"
            )

        updated_version = self.get_version(
            bin_name,
            abspath=updated_abspath,
            quiet=True,
            no_cache=True,
        )
        if not quiet:
            assert updated_version, (
                f"❌ {self.__class__.__name__} Unable to find version for {bin_name} after updating. ABSPATH={updated_abspath} LOG={update_log}"
            )
        self._assert_min_version_satisfied(
            bin_name=bin_name,
            action="update",
            loaded_version=updated_version,
            min_version=min_version,
        )

        sha256 = (
            self.get_sha256(bin_name, abspath=updated_abspath, no_cache=True)
            or UNKNOWN_SHA256
        )

        if updated_abspath and updated_version:
            logger.info(
                format_loaded_binary(
                    "⬆ Updated",
                    updated_abspath,
                    updated_version,
                    self,
                ),
                extra={"abx_cli_duplicate_stdout": True},
            )
            return ShallowBinary.model_validate(
                {
                    "name": bin_name,
                    "binprovider": self,
                    "abspath": updated_abspath,
                    "version": updated_version,
                    "sha256": sha256,
                    "binproviders": [self],
                },
            )

        return None

    @final
    @log_method_call(include_result=True)
    @validate_call
    def uninstall(
        self,
        bin_name: BinName,
        quiet: bool = False,
        no_cache: bool = False,
        dry_run: bool | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> bool:
        if dry_run is not None and dry_run != self.dry_run:
            return self.get_provider_with_overrides(dry_run=dry_run).uninstall(
                bin_name=bin_name,
                quiet=quiet,
                no_cache=no_cache,
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
                min_version=min_version,
            )
        postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        install_args = self.get_install_args(bin_name, quiet=quiet, no_cache=no_cache)
        self.setup_PATH()
        uninstall_result = None
        exec_log_prefix_token = ACTIVE_EXEC_LOG_PREFIX.set(
            f"🗑️ Uninstalling {bin_name} via {self.name}...",
        )
        logger.info(ACTIVE_EXEC_LOG_PREFIX.get())
        try:
            uninstall_result = cast(
                ActionFuncReturnValue,
                self._call_handler_for_action(
                    bin_name=bin_name,
                    handler_type="uninstall",
                    install_args=install_args,
                    packages=install_args,
                    no_cache=no_cache,
                    postinstall_scripts=postinstall_scripts,
                    min_release_age=min_release_age,
                    min_version=min_version,
                    timeout=self.install_timeout,
                ),
            )
        except Exception:
            if not quiet:
                raise
            return False
        finally:
            ACTIVE_EXEC_LOG_PREFIX.reset(exec_log_prefix_token)

        self.invalidate_cache(bin_name)

        if self.dry_run:
            return True

        if uninstall_result is not False:
            logger.info("🗑️ Uninstalled %s via %s", bin_name, self.name)
        return uninstall_result is not False

    @final
    @log_method_call(include_result=True)
    @validate_call
    def load(
        self,
        bin_name: BinName,
        quiet: bool = True,
        no_cache: bool = False,
    ) -> ShallowBinary | None:
        # Fast path: check persistent on-disk metadata cache first.
        # This avoids shelling out to ``binary --version`` (~100ms each)
        # on repeated load() calls across process restarts.
        if not no_cache:
            cached = metadata_cache.get(self.name, bin_name, self.install_root)
            if cached is not None:
                cached_abspath, cached_version, cached_sha256 = cached
                try:
                    result = ShallowBinary.model_validate(
                        {
                            "name": bin_name,
                            "binprovider": self,
                            "abspath": cached_abspath,
                            "version": SemVer.parse(cached_version),
                            "sha256": cached_sha256,
                            "binproviders": [self],
                        },
                    )
                    if result.is_valid:
                        logger.debug(
                            "☑️ Loaded %s from metadata cache (%s)",
                            bin_name,
                            self.name,
                        )
                        return result
                except Exception:
                    pass

        installed_abspath = self.get_abspath(bin_name, quiet=quiet, no_cache=no_cache)
        if not installed_abspath:
            return None

        installed_version = self.get_version(
            bin_name,
            abspath=installed_abspath,
            quiet=quiet,
            no_cache=no_cache,
        )
        if not installed_version:
            return None

        sha256 = (
            self.get_sha256(bin_name, abspath=installed_abspath) or UNKNOWN_SHA256
        )  # not ideal to store UNKNOWN_SHA256 but it's better than nothing and this value isn't critical

        result = ShallowBinary.model_validate(
            {
                "name": bin_name,
                "binprovider": self,
                "abspath": installed_abspath,
                "version": installed_version,
                "sha256": sha256,
                "binproviders": [self],
            },
        )

        # Persist to on-disk cache for fast loading in future processes
        metadata_cache.set(
            self.name,
            bin_name,
            self.install_root,
            installed_abspath,
            str(installed_version),
            sha256,
        )

        logger.info(
            format_loaded_binary(
                "☑️ Loaded",
                installed_abspath,
                installed_version,
                self,
            ),
            extra={"abx_cli_duplicate_stdout": True},
        )
        return result


class EnvProvider(BinProvider):
    name: BinProviderName = "env"
    INSTALLER_BIN: BinName = "which"
    PATH: PATHStr = DEFAULT_ENV_PATH  # add dir containing python to $PATH

    overrides: "BinProviderOverrides" = {
        "*": {
            "version": "self.default_version_handler",
            "abspath": "self.default_abspath_handler",
            "install_args": "self.default_install_args_handler",
            "install": "self.install_noop",
            "update": "self.update_noop",
            "uninstall": "self.uninstall_noop",
        },
        "python": {
            "abspath": Path(sys.executable),
            "version": "{}.{}.{}".format(*sys.version_info[:3]),
        },
    }

    def supports_min_release_age(self, action: Literal["install", "update"]) -> bool:
        return False

    def supports_postinstall_disable(
        self,
        action: Literal["install", "update"],
    ) -> bool:
        return False

    @remap_kwargs({"packages": "install_args"})
    @log_method_call(include_result=True)
    def install_noop(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> str:
        """The env BinProvider is ready-only and does not install any packages, so this is a no-op"""
        return "env is ready-only and just checks for existing binaries in $PATH"

    @remap_kwargs({"packages": "install_args"})
    @log_method_call(include_result=True)
    def update_noop(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> str:
        return "env is read-only and just checks for existing binaries in $PATH"

    @remap_kwargs({"packages": "install_args"})
    @log_method_call(include_result=True)
    def uninstall_noop(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> bool:
        return False


############################################################################################################


AbspathFuncReturnValue = str | HostBinPath | None
VersionFuncReturnValue = (
    str | tuple[int, ...] | tuple[str, ...] | SemVer | None
)  # SemVer is a subclass of NamedTuple
InstallArgsFuncReturnValue = list[str] | tuple[str, ...] | str | InstallArgs | None
PackagesFuncReturnValue = InstallArgsFuncReturnValue
InstallFuncReturnValue = str | None
ActionFuncReturnValue = str | bool | None
ProviderFuncReturnValue = (
    AbspathFuncReturnValue
    | VersionFuncReturnValue
    | InstallArgsFuncReturnValue
    | InstallFuncReturnValue
    | ActionFuncReturnValue
)


@runtime_checkable
class AbspathFuncWithArgs(Protocol):
    def __call__(
        _self,
        binprovider: "BinProvider",
        bin_name: BinName,
        **context,
    ) -> "AbspathFuncReturnValue": ...


@runtime_checkable
class VersionFuncWithArgs(Protocol):
    def __call__(
        _self,
        binprovider: "BinProvider",
        bin_name: BinName,
        **context,
    ) -> "VersionFuncReturnValue": ...


@runtime_checkable
class InstallArgsFuncWithArgs(Protocol):
    def __call__(
        _self,
        binprovider: "BinProvider",
        bin_name: BinName,
        **context,
    ) -> "InstallArgsFuncReturnValue": ...


PackagesFuncWithArgs = InstallArgsFuncWithArgs


@runtime_checkable
class InstallFuncWithArgs(Protocol):
    def __call__(
        _self,
        binprovider: "BinProvider",
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        **context: Any,
    ) -> "InstallFuncReturnValue": ...


@runtime_checkable
class ActionFuncWithArgs(Protocol):
    def __call__(
        _self,
        binprovider: "BinProvider",
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        **context: Any,
    ) -> "ActionFuncReturnValue": ...


AbspathFuncWithNoArgs = Callable[[], AbspathFuncReturnValue]
VersionFuncWithNoArgs = Callable[[], VersionFuncReturnValue]
InstallArgsFuncWithNoArgs = Callable[[], InstallArgsFuncReturnValue]
PackagesFuncWithNoArgs = InstallArgsFuncWithNoArgs
InstallFuncWithNoArgs = Callable[[], InstallFuncReturnValue]
ActionFuncWithNoArgs = Callable[[], ActionFuncReturnValue]

AbspathHandlerValue = (
    SelfMethodName
    | AbspathFuncWithNoArgs
    | AbspathFuncWithArgs
    | AbspathFuncReturnValue
)
VersionHandlerValue = (
    SelfMethodName
    | VersionFuncWithNoArgs
    | VersionFuncWithArgs
    | VersionFuncReturnValue
)
InstallArgsHandlerValue = (
    SelfMethodName
    | InstallArgsFuncWithNoArgs
    | InstallArgsFuncWithArgs
    | InstallArgsFuncReturnValue
)
PackagesHandlerValue = InstallArgsHandlerValue
InstallHandlerValue = (
    SelfMethodName
    | InstallFuncWithNoArgs
    | InstallFuncWithArgs
    | InstallFuncReturnValue
)
ActionHandlerValue = (
    SelfMethodName | ActionFuncWithNoArgs | ActionFuncWithArgs | ActionFuncReturnValue
)

HandlerType = Literal[
    "abspath",
    "version",
    "install_args",
    "packages",
    "install",
    "update",
    "uninstall",
]
HandlerValue = (
    AbspathHandlerValue
    | VersionHandlerValue
    | InstallArgsHandlerValue
    | InstallHandlerValue
    | ActionHandlerValue
)
HandlerReturnValue = (
    AbspathFuncReturnValue
    | VersionFuncReturnValue
    | InstallArgsFuncReturnValue
    | InstallFuncReturnValue
    | ActionFuncReturnValue
)


class HandlerDict(TypedDict, total=False):
    abspath: AbspathHandlerValue
    version: VersionHandlerValue
    install_args: InstallArgsHandlerValue
    packages: InstallArgsHandlerValue
    install: InstallHandlerValue
    update: ActionHandlerValue
    uninstall: ActionHandlerValue


# Binary.overrides map BinProviderName:HandlerType:Handler    {'brew': {'install_args': [...]}}
BinaryOverrides = dict[BinProviderName, HandlerDict]

# BinProvider.overrides map BinName:HandlerType:Handler       {'wget': {'install_args': [...]}}
BinProviderOverrides = dict[BinName | Literal["*"], HandlerDict]

# Resolve forward refs at import time so downstream subclasses don't need to call model_rebuild().
ShallowBinary.model_rebuild(_types_namespace=globals())
BinProvider.model_rebuild(_types_namespace=globals())
EnvProvider.model_rebuild(_types_namespace=globals())
