__package__ = "abx_pkg"

import os
import sys
import pwd
import inspect
import shutil
import hashlib
import platform
import subprocess
import functools
from types import SimpleNamespace

from typing import Callable, Optional, Iterable, List, cast, final, Dict, Any, Tuple, Literal, Protocol, runtime_checkable, ParamSpec, TypeVar, Mapping

from typing_extensions import Self, TypedDict
from pathlib import Path

from pydantic_core import ValidationError
from pydantic import BaseModel, Field, TypeAdapter, validate_call, ConfigDict, InstanceOf, computed_field, model_validator

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

################## GLOBALS ##########################################

OPERATING_SYSTEM = platform.system().lower()
DEFAULT_PATH = "/home/linuxbrew/.linuxbrew/bin:/opt/homebrew/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_ENV_PATH = os.environ.get("PATH", DEFAULT_PATH)
PYTHON_BIN_DIR = str(Path(sys.executable).parent)

if PYTHON_BIN_DIR not in DEFAULT_ENV_PATH:
    DEFAULT_ENV_PATH = PYTHON_BIN_DIR + ":" + DEFAULT_ENV_PATH

UNKNOWN_ABSPATH = Path('/usr/bin/true')
UNKNOWN_VERSION = cast(SemVer, SemVer.parse('999.999.999'))

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
        
        if bin_name in method_cache and not kwargs.get('nocache'):
            # print('USING CACHED VALUE:', f'{self.__class__.__name__}.{method_name}({bin_name}, {kwargs}) -> {method_cache[bin_name]}')
            return method_cache[bin_name]
        
        return_value = binprovider_method(self, bin_name, **kwargs)
        
        if return_value and return_value not in NEVER_CACHE:
            self._cache[method_name][bin_name] = return_value
        return return_value
    
    cached_function.__name__ = f'{method_name}_cached'
    
    return cached_function


P = ParamSpec("P")
R = TypeVar("R")

def remap_kwargs(renamed_kwargs: Mapping[str, str]) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            mapped_kwargs = dict(kwargs)
            for old_name, new_name in renamed_kwargs.items():
                if old_name in mapped_kwargs:
                    mapped_kwargs.setdefault(new_name, mapped_kwargs[old_name])
                    mapped_kwargs.pop(old_name, None)
            return func(*args, **cast(Any, mapped_kwargs))

        return cast(Callable[P, R], wrapper)

    return decorator



class ShallowBinary(BaseModel):
    """
    Shallow version of Binary used as a return type for BinProvider methods (e.g. load_or_install()).
    (doesn't implement full Binary interface, but can be used to populate a full loaded Binary instance)
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_defaults=True, validate_assignment=False, from_attributes=True, arbitrary_types_allowed=True)

    name: BinName = ""
    description: str = ""

    binproviders_supported: List[InstanceOf["BinProvider"]] = Field(default_factory=list, alias="binproviders")
    overrides: 'BinaryOverrides' = Field(default_factory=dict)

    loaded_binprovider: InstanceOf["BinProvider"] = Field(alias="binprovider")
    loaded_abspath: HostBinPath = Field(alias="abspath")
    loaded_version: SemVer = Field(alias="version")
    loaded_sha256: Sha256 = Field(alias="sha256")

    def __getattr__(self, item):
        """Allow accessing fields as attributes by both field name and alias name"""
        for field, meta in type(self).model_fields.items():
            if meta.alias == item:
                return getattr(self, field)
        return super().__getattr__(item)

    @model_validator(mode="after")
    def validate(self) -> Self:
        self.description = self.description or self.name
        return self

    @computed_field  # type: ignore[misc]  # see mypy issue #1362
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

    @computed_field  # type: ignore[misc]  # see mypy issue #1362
    @property
    def is_executable(self) -> bool:
        try:
            assert self.loaded_abspath and path_is_executable(self.loaded_abspath)
            return True
        except (ValidationError, AssertionError):
            return False

    @computed_field  # type: ignore[misc]  # see mypy issue #1362
    @property
    def is_script(self) -> bool:
        try:
            assert self.loaded_abspath and path_is_script(self.loaded_abspath)
            return True
        except (ValidationError, AssertionError):
            return False

    @computed_field  # type: ignore[misc]  # see mypy issue #1362
    @property
    def is_valid(self) -> bool:
        return bool(self.name and self.loaded_abspath and self.loaded_version and (self.is_executable or self.is_script))

    @computed_field
    @property
    def bin_dir(self) -> BinDirPath | None:
        if not self.loaded_abspath:
            return None
        return TypeAdapter(BinDirPath).validate_python(self.loaded_abspath.parent)

    @computed_field
    @property
    def loaded_respath(self) -> HostBinPath | None:
        return self.loaded_abspath and self.loaded_abspath.resolve()

    # @validate_call
    def exec(
        self, bin_name: BinName | HostBinPath | None = None, cmd: Iterable[str | Path | int | float | bool] = (), cwd: str | Path = ".", quiet=False, **kwargs
    ) -> subprocess.CompletedProcess:
        bin_name = str(bin_name or self.loaded_abspath or self.name)
        if bin_name == self.name:
            assert self.loaded_abspath, "Binary must have a loaded_abspath, make sure to load_or_install() first"
            assert self.loaded_version, "Binary must have a loaded_version, make sure to load_or_install() first"
        assert os.path.isdir(cwd) and os.access(cwd, os.R_OK), f"cwd must be a valid, accessible directory: {cwd}"
        cmd = [str(bin_name), *(str(arg) for arg in cmd)]
        if not quiet:
            print('$', ' '.join(cmd), file=sys.stderr)
        return subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd), **kwargs)


DEFAULT_OVERRIDES = {
    '*': {
        'version': 'self.default_version_handler',
        'abspath': 'self.default_abspath_handler',
        'install_args': 'self.default_install_args_handler',
        'install': 'self.default_install_handler',
        'update': 'self.default_update_handler',
        'uninstall': 'self.default_uninstall_handler',
    },
}


class BinProvider(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True, validate_defaults=True, validate_assignment=False, from_attributes=True, revalidate_instances='always', arbitrary_types_allowed=True)
    name: BinProviderName = ''

    PATH: PATHStr = Field(default=str(Path(sys.executable).parent), repr=False)        # e.g.  '/opt/homebrew/bin:/opt/archivebox/bin'
    INSTALLER_BIN: BinName = 'env'
    
    euid: Optional[int] = None
    
    overrides: 'BinProviderOverrides' = Field(default=DEFAULT_OVERRIDES, repr=False, exclude=True)

    _dry_run: bool = False
    _install_timeout: int = 120
    _version_timeout: int = 10
    _cache: Dict[str, Dict[str, Any]] | None = None
    _INSTALLER_BIN_ABSPATH: HostBinPath | None = None    # speed optimization only, faster to cache the abspath than to recompute it on every access
    _INSTALLER_BINARY: ShallowBinary | None = None       # speed optimization only, faster to cache the binary than to recompute it on every access
    
    def __eq__(self, other: Any) -> bool:
        try:
            return dict(self) == dict(other)             # only compare pydantic fields, ignores classvars/@properties/@cached_properties/_fields/etc.
        except Exception:
            return False

    @staticmethod
    def uid_has_passwd_entry(uid: int) -> bool:
        try:
            pwd.getpwuid(uid)
        except KeyError:
            return False
        return True

    def detect_euid(self, owner_paths: Iterable[str | Path | None]=(), preserve_root: bool=False) -> int:
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
                pw_dir=os.environ.get('HOME', '/tmp'),
                pw_name=os.environ.get('USER') or os.environ.get('LOGNAME') or str(uid),
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
        
        abspath = bin_abspath(self.INSTALLER_BIN, PATH=self.PATH) or bin_abspath(self.INSTALLER_BIN)  # find self.INSTALLER_BIN abspath using environment path
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
        
        return ShallowBinary(
            name=self.INSTALLER_BIN,
            abspath=abspath,
            binprovider=env,
            version=version,
            sha256=sha256,
        )
    
    @computed_field
    @property
    def is_valid(self) -> bool:
        return bool(self.INSTALLER_BIN_ABSPATH)

    @final
    # @validate_call(config={'arbitrary_types_allowed': True})
    def get_provider_with_overrides(self, overrides: Optional['BinProviderOverrides']=None, dry_run: bool=False, install_timeout: int | None=None, version_timeout: int | None=None) -> Self:
        # created an updated copy of the BinProvider with the overrides applied, then get the handlers on it.
        # important to do this so that any subsequent calls to handler functions down the call chain
        # still have access to the overrides, we don't have to have to pass them down as args all the way down the stack
        
        updated_binprovider: Self = self.model_copy()
        
        # main binary-specific overrides for [abspath, version, install_args, install, update, uninstall]
        overrides = overrides or {}
        
        # extra overrides that are also configurable, can add more in the future as-needed for tunable options
        updated_binprovider._dry_run = dry_run
        updated_binprovider._install_timeout = install_timeout or self._install_timeout
        updated_binprovider._version_timeout = version_timeout or self._version_timeout
        
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
    def _get_handler_keys(self, handler_type: 'HandlerType') -> Tuple['HandlerType', ...]:
        if handler_type in ('install_args', 'packages'):
            return ('install_args', 'packages')
        return (handler_type,)

    def _get_handler_for_action(self, bin_name: BinName, handler_type: 'HandlerType') -> Callable[..., 'HandlerReturnValue']:
        """
        Get the handler func for a given key + Dict of handler callbacks + fallback default handler.
        e.g. _get_handler_for_action(bin_name='yt-dlp', 'install', default_handler=self.default_install_handler, ...) -> Callable
        """

        handler: HandlerValue | None = None
        for overrides_for_bin in (self.overrides.get(bin_name, {}), self.overrides.get('*', {})):
            for handler_key in self._get_handler_keys(handler_type):
                handler = overrides_for_bin.get(handler_key)
                if handler:
                    break
            if handler:
                break
        # print('getting handler for action', bin_name, handler_type, handler_func)
        assert handler, f'BinProvider(name={self.name}) has no {handler_type} handler implemented for Binary(name={bin_name})'

        # if handler_func is already a callable, return it directly
        if isinstance(handler, Callable):
            handler_func: Callable[..., HandlerReturnValue] = handler
            return handler_func

        # if handler_func is string reference to a function on self, swap it for the actual function
        elif isinstance(handler, str) and (handler.startswith('self.') or handler.startswith('BinProvider.')):
            # special case, allow dotted path references to methods on self (where self refers to the BinProvider)
            handler_method: Callable[..., HandlerReturnValue] = getattr(self, handler.split('self.', 1)[-1])
            return handler_method

        # if handler_func is any other value, treat is as a literal and return a func that provides the literal
        literal_value = TypeAdapter(HandlerReturnValue).validate_python(handler)
        handler_func: Callable[..., HandlerReturnValue] = lambda: literal_value         # noqa: E731
        return handler_func

    # @validate_call
    def _get_compatible_kwargs(self, handler_func: Callable[..., 'HandlerReturnValue'], kwargs: Dict[str, Any]) -> Dict[str, Any]:
        if not kwargs:
            return kwargs

        signature = inspect.signature(handler_func)
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
            return kwargs

        accepted_kwargs = set(signature.parameters)
        return {
            key: value
            for key, value in kwargs.items()
            if key in accepted_kwargs
        }

    def _call_handler_for_action(self, bin_name: BinName, handler_type: 'HandlerType', **kwargs) -> 'HandlerReturnValue':
        handler_func: Callable[..., HandlerReturnValue] = self._get_handler_for_action(
            bin_name=bin_name,           # e.g. 'yt-dlp', or 'wget', etc.
            handler_type=handler_type,   # e.g. abspath, version, install_args, install
        )

        # def timeout_handler(signum, frame):
            # raise TimeoutError(f'{self.__class__.__name__} Timeout while running {handler_type} for Binary {bin_name}')

        # signal ONLY WORKS IN MAIN THREAD, not a viable solution for timeout enforcement! breaks in prod
        # signal.signal(signal.SIGALRM, handler=timeout_handler)
        # signal.alarm(timeout)
        try:
            if not func_takes_args_or_kwargs(handler_func):
                # if it's a pure argless lambda/func, dont pass bin_path and other **kwargs
                handler_func_without_args = cast(Callable[[], HandlerReturnValue], handler_func)
                return handler_func_without_args()

            handler_func = cast(Callable[..., HandlerReturnValue], handler_func)
            compatible_kwargs = self._get_compatible_kwargs(handler_func, kwargs)
            if hasattr(handler_func, '__self__'):
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
    def default_abspath_handler(self, bin_name: BinName | HostBinPath, **context) -> 'AbspathFuncReturnValue':  # aka str | Path | None
        # print(f'[*] {self.__class__.__name__}: Getting abspath for {bin_name}...')

        if not self.PATH:
            return None
        
        return bin_abspath(bin_name, PATH=self.PATH)
    
    # @validate_call
    def default_version_handler(self, bin_name: BinName, abspath: Optional[HostBinPath]=None, **context) -> 'VersionFuncReturnValue':  # aka List[str] | Tuple[str, ...]
        
        abspath = abspath or self.get_abspath(bin_name, quiet=True)
        if not abspath:
            return None

        # print(f'[*] {self.__class__.__name__}: Getting version for {bin_name}...')
        
        validation_err = None
        
        # Attempt 1: $ <bin_name> --version
        dash_dash_version_result = self.exec(bin_name=abspath, cmd=['--version'], timeout=self._version_timeout, quiet=True)
        dash_dash_version_out = dash_dash_version_result.stdout.strip()
        try:
            version = SemVer.parse(dash_dash_version_out)
            assert version, f"Could not parse version from $ {bin_name} --version: {dash_dash_version_result.stdout}\n{dash_dash_version_result.stderr}\n".strip()
            return version
        except (ValidationError, AssertionError) as err:
            validation_err = err
        
        # Attempt 2: $ <bin_name> -version
        dash_version_out = self.exec(bin_name=abspath, cmd=["-version"], timeout=self._version_timeout, quiet=True).stdout.strip()
        try:
            version = SemVer.parse(dash_version_out)
            assert version, f"Could not parse version from $ {bin_name} -version: {dash_version_out}".strip()
            return version
        except (ValidationError, AssertionError) as err:
            validation_err = validation_err or err
        
        # Attempt 3: $ <bin_name> -v
        dash_v_out = self.exec(bin_name=abspath, cmd=["-v"], timeout=self._version_timeout, quiet=True).stdout.strip()
        try:
            version = SemVer.parse(dash_v_out)
            assert version, f"Could not parse version from $ {bin_name} -v: {dash_v_out}".strip()
            return version
        except (ValidationError, AssertionError) as err:
            validation_err = validation_err or err
        
        raise ValueError(
            f"Unable to find {bin_name} version from {bin_name} --version, -version or -v output\n{dash_dash_version_out or dash_version_out or dash_v_out}".strip()
        ) from validation_err

    # @validate_call
    def default_install_args_handler(self, bin_name: BinName, **context) -> 'InstallArgsFuncReturnValue':     # aka List[str] aka InstallArgs
        # print(f'[*] {self.__class__.__name__}: Getting install command for {bin_name}')
        # ... install command calculation logic here
        return [bin_name]

    def default_packages_handler(self, bin_name: BinName, **context) -> 'InstallArgsFuncReturnValue':
        return self.default_install_args_handler(bin_name, **context)

    # @validate_call
    @remap_kwargs({'packages': 'install_args'})
    def default_install_handler(self, bin_name: BinName, install_args: Optional[InstallArgs]=None, **context) -> 'InstallFuncReturnValue':      # aka str
        self.setup()
        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(f'{self.name} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)')

        # print(f'[*] {self.__class__.__name__}: Installing {bin_name}: {self.INSTALLER_BIN_ABSPATH} {install_args}')

        # ... override the default install logic here ...
        
        # proc = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=['install', *install_args], timeout=self._install_timeout)
        # if not proc.returncode == 0:
        #     print(proc.stdout.strip())
        #     print(proc.stderr.strip())
        #     raise Exception(f'{self.name} Failed to install {bin_name}: {proc.stderr.strip()}\n{proc.stdout.strip()}')

        return f'{self.name} BinProvider does not implement any install method'

    # @validate_call
    @remap_kwargs({'packages': 'install_args'})
    def default_update_handler(self, bin_name: BinName, install_args: Optional[InstallArgs]=None, **context) -> 'ActionFuncReturnValue':
        return f'{self.name} BinProvider does not implement any update method'

    # @validate_call
    @remap_kwargs({'packages': 'install_args'})
    def default_uninstall_handler(self, bin_name: BinName, install_args: Optional[InstallArgs]=None, **context) -> 'ActionFuncReturnValue':
        return False

    def invalidate_cache(self, bin_name: BinName) -> None:
        if not self._cache:
            return
        for method_cache in self._cache.values():
            method_cache.pop(bin_name, None)


    def setup_PATH(self) -> None:
        for path in reversed(self.PATH.split(':')):
            if path not in sys.path:
                sys.path.insert(0, path)   # e.g. /opt/archivebox/bin:/bin:/usr/local/bin:...

    # @validate_call
    def exec(self, bin_name: BinName | HostBinPath, cmd: Iterable[str | Path | int | float | bool]=(), cwd: Path | str='.', quiet=False, **kwargs) -> subprocess.CompletedProcess:
        if shutil.which(str(bin_name)):
            bin_abspath = bin_name
        else:
            bin_abspath = self.get_abspath(str(bin_name))
        assert bin_abspath, f'BinProvider {self.name} cannot execute bin_name {bin_name} because it could not find its abspath. (Did {self.__class__.__name__}.load_or_install({bin_name}) fail?)'
        assert os.access(cwd, os.R_OK) and os.path.isdir(cwd), f'cwd must be a valid, accessible directory: {cwd}'
        cmd = [str(bin_abspath), *(str(arg) for arg in cmd)]
        if not quiet:
            prefix = 'DRY RUN: $' if self._dry_run else '$'
            print(prefix, ' '.join(cmd), file=sys.stderr)
            
        # https://stackoverflow.com/a/6037494/2156113
        # copy env and modify it to run the subprocess as the the designated user
        env = kwargs.pop('env', {}) or os.environ.copy()
        pw_record = self.get_pw_record(self.EUID)
        run_as_uid     = pw_record.pw_uid
        run_as_gid     = pw_record.pw_gid
        # update environment variables so that subprocesses dont try to write to /root home directory
        # for things like cache dirs, logs, etc. npm/pip/etc. often try to write to $HOME
        env['PWD']      = str(cwd)
        env['HOME']     = pw_record.pw_dir
        env['LOGNAME']  = pw_record.pw_name
        env['USER']     = pw_record.pw_name
        
        def drop_privileges():
            try:
                os.setuid(run_as_uid)
                os.setgid(run_as_gid)
            except Exception:
                pass
        
        if self._dry_run:
            return subprocess.CompletedProcess(cmd, 0, '', 'skipped (dry run)')
        
        return subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd), env=env, preexec_fn=drop_privileges, **kwargs)


    # CALLING API, DONT OVERRIDE THESE:

    @final
    @binprovider_cache
    # @validate_call
    def get_abspaths(self, bin_name: BinName, nocache: bool=False) -> List[HostBinPath]:
        return bin_abspaths(bin_name, PATH=self.PATH)

    @final
    @binprovider_cache
    # @validate_call
    def get_sha256(self, bin_name: BinName, abspath: Optional[HostBinPath]=None, nocache: bool=False) -> Sha256 | None:
        """Get the sha256 hash of the binary at the given abspath (or equivalent hash of the underlying package)"""
        
        abspath = abspath or self.get_abspath(bin_name, nocache=nocache)
        if not abspath or not os.access(abspath, os.R_OK):
            return None
        
        if sys.version_info >= (3, 11):
            with open(abspath, "rb", buffering=0) as f:
                return TypeAdapter(Sha256).validate_python(hashlib.file_digest(f, 'sha256').hexdigest())
        
        hash_sha256 = hashlib.sha256()
        with open(abspath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        return TypeAdapter(Sha256).validate_python(hash_sha256.hexdigest())

    @final
    @binprovider_cache
    # @validate_call
    def get_abspath(self, bin_name: BinName, quiet: bool=False, nocache: bool=False) -> HostBinPath | None:
        self.setup_PATH()
        abspath = None
        try:
            abspath = cast(AbspathFuncReturnValue, self._call_handler_for_action(bin_name=bin_name, handler_type='abspath'))
        except Exception:
            if not quiet:
                raise
        if not abspath:
            return None
        result = TypeAdapter(HostBinPath).validate_python(abspath)
        return result

    @final
    @binprovider_cache
    # @validate_call
    def get_version(self, bin_name: BinName, abspath: Optional[HostBinPath]=None, quiet: bool=False, nocache: bool=False) -> SemVer | None:
        version = None
        try:
            version = cast(VersionFuncReturnValue, self._call_handler_for_action(bin_name=bin_name, handler_type='version', abspath=abspath))
        except Exception:
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
    def get_install_args(self, bin_name: BinName, quiet: bool=False, nocache: bool=False) -> InstallArgs:
        install_args = None
        try:
            install_args = cast(InstallArgsFuncReturnValue, self._call_handler_for_action(bin_name=bin_name, handler_type='install_args'))
        except Exception:
            if not quiet:
                raise

        if not install_args:
            install_args = [bin_name]
        result = TypeAdapter(InstallArgs).validate_python(install_args)
        return result

    def get_packages(self, bin_name: BinName, quiet: bool=False, nocache: bool=False) -> InstallArgs:
        return self.get_install_args(bin_name, quiet=quiet, nocache=nocache)

    def setup(self) -> None:
        """Override this to do any setup steps needed before installing packaged (e.g. create a venv, init an npm prefix, etc.)"""
        pass

    @final
    @binprovider_cache
    @validate_call
    def install(self, bin_name: BinName, quiet: bool=False, nocache: bool=False) -> ShallowBinary | None:
        self.setup()
        
        install_args = self.get_install_args(bin_name, quiet=quiet, nocache=nocache)
        
        self.setup_PATH()
        install_log = None
        try:
            install_log = cast(InstallFuncReturnValue, self._call_handler_for_action(bin_name=bin_name, handler_type='install', install_args=install_args, packages=install_args))
        except Exception as err:
            install_log = f'{self.__class__.__name__} Failed to install {bin_name}, got {err.__class__.__name__}: {err}'
            if not quiet:
                raise
            
        if self._dry_run:
            # return fake ShallowBinary if we're just doing a dry run
            # no point trying to get real abspath or version if nothing was actually installed
            return ShallowBinary(
                name=bin_name,
                binprovider=self,
                abspath=Path(shutil.which(bin_name) or UNKNOWN_ABSPATH),
                version=cast(SemVer, UNKNOWN_VERSION),
                sha256=UNKNOWN_SHA256, binproviders=[self],
            )

        installed_abspath = self.get_abspath(bin_name, quiet=True, nocache=nocache)
        if not quiet:
            assert installed_abspath, f'{self.__class__.__name__} Unable to find abspath for {bin_name} after installing. PATH={self.PATH} LOG={install_log}'

        installed_version = self.get_version(bin_name, abspath=installed_abspath, quiet=True, nocache=nocache)
        if not quiet:
            assert installed_version, f'{self.__class__.__name__} Unable to find version for {bin_name} after installing. ABSPATH={installed_abspath} LOG={install_log}'
        
        sha256 = self.get_sha256(bin_name, abspath=installed_abspath, nocache=nocache) or UNKNOWN_SHA256
        
        if (installed_abspath and installed_version):
            # installed binary is valid and ready to use
            result = ShallowBinary(
                name=bin_name,
                binprovider=self,
                abspath=installed_abspath,
                version=installed_version,
                sha256=sha256,
                binproviders=[self],
            )
        else:
            result = None

        return result

    @final
    @validate_call
    def update(self, bin_name: BinName, quiet: bool=False, nocache: bool=False) -> ShallowBinary | None:
        self.setup()

        install_args = self.get_install_args(bin_name, quiet=quiet, nocache=nocache)

        self.setup_PATH()
        update_log = None
        try:
            update_log = cast(ActionFuncReturnValue, self._call_handler_for_action(bin_name=bin_name, handler_type='update', install_args=install_args, packages=install_args))
        except Exception as err:
            update_log = f'{self.__class__.__name__} Failed to update {bin_name}, got {err.__class__.__name__}: {err}'
            if not quiet:
                raise

        if self._dry_run:
            return ShallowBinary(
                name=bin_name,
                binprovider=self,
                abspath=Path(shutil.which(bin_name) or UNKNOWN_ABSPATH),
                version=cast(SemVer, UNKNOWN_VERSION),
                sha256=UNKNOWN_SHA256,
                binproviders=[self],
            )

        self.invalidate_cache(bin_name)

        updated_abspath = self.get_abspath(bin_name, quiet=True, nocache=True)
        if not quiet:
            assert updated_abspath, f'{self.__class__.__name__} Unable to find abspath for {bin_name} after updating. PATH={self.PATH} LOG={update_log}'

        updated_version = self.get_version(bin_name, abspath=updated_abspath, quiet=True, nocache=True)
        if not quiet:
            assert updated_version, f'{self.__class__.__name__} Unable to find version for {bin_name} after updating. ABSPATH={updated_abspath} LOG={update_log}'

        sha256 = self.get_sha256(bin_name, abspath=updated_abspath, nocache=True) or UNKNOWN_SHA256

        if updated_abspath and updated_version:
            return ShallowBinary(
                name=bin_name,
                binprovider=self,
                abspath=updated_abspath,
                version=updated_version,
                sha256=sha256,
                binproviders=[self],
            )

        return None

    @final
    @validate_call
    def uninstall(self, bin_name: BinName, quiet: bool=False, nocache: bool=False) -> bool:
        install_args = self.get_install_args(bin_name, quiet=quiet, nocache=nocache)

        self.setup_PATH()
        uninstall_result = None
        try:
            uninstall_result = cast(ActionFuncReturnValue, self._call_handler_for_action(bin_name=bin_name, handler_type='uninstall', install_args=install_args, packages=install_args))
        except Exception:
            if not quiet:
                raise
            return False

        self.invalidate_cache(bin_name)

        if self._dry_run:
            return True

        return uninstall_result is not False

    @final
    @validate_call
    def load(self, bin_name: BinName, quiet: bool=True, nocache: bool=False) -> ShallowBinary | None:
        installed_abspath = self.get_abspath(bin_name, quiet=quiet, nocache=nocache)
        if not installed_abspath:
            return None

        installed_version = self.get_version(bin_name, abspath=installed_abspath, quiet=quiet, nocache=nocache)
        if not installed_version:
            return None
        
        sha256 = self.get_sha256(bin_name, abspath=installed_abspath) or UNKNOWN_SHA256  # not ideal to store UNKNOWN_SHA256but it's better than nothing and this value isnt critical
        
        return ShallowBinary(
            name=bin_name,
            binprovider=self,
            abspath=installed_abspath,
            version=installed_version,
            sha256=sha256,
            binproviders=[self],
        )

    @final
    @validate_call
    def load_or_install(self, bin_name: BinName, quiet: bool=False, nocache: bool=False) -> ShallowBinary | None:
        installed = self.load(bin_name=bin_name, quiet=True, nocache=nocache)
        if not installed:
            installed = self.install(bin_name=bin_name, quiet=quiet, nocache=nocache)
        return installed



class EnvProvider(BinProvider):
    name: BinProviderName = 'env'
    INSTALLER_BIN: BinName = 'which'
    PATH: PATHStr = DEFAULT_ENV_PATH     # add dir containing python to $PATH

    overrides: 'BinProviderOverrides' = {
        '*': {
            **BinProvider.model_fields['overrides'].default['*'],
            'install': 'self.install_noop',
            'update': 'self.update_noop',
            'uninstall': 'self.uninstall_noop',
        },
        'python': {
            'abspath': Path(sys.executable),
            'version': '{}.{}.{}'.format(*sys.version_info[:3]),
        },
    }

    @remap_kwargs({'packages': 'install_args'})
    def install_noop(self, bin_name: BinName, install_args: Optional[InstallArgs]=None, **context) -> str:
        """The env BinProvider is ready-only and does not install any packages, so this is a no-op"""
        return 'env is ready-only and just checks for existing binaries in $PATH'

    @remap_kwargs({'packages': 'install_args'})
    def update_noop(self, bin_name: BinName, install_args: Optional[InstallArgs]=None, **context) -> str:
        return 'env is read-only and just checks for existing binaries in $PATH'

    @remap_kwargs({'packages': 'install_args'})
    def uninstall_noop(self, bin_name: BinName, install_args: Optional[InstallArgs]=None, **context) -> bool:
        return False

############################################################################################################



AbspathFuncReturnValue = str | HostBinPath | None
VersionFuncReturnValue = str | Tuple[int, ...] | Tuple[str, ...] | SemVer | None     # SemVer is a subclass of NamedTuple
InstallArgsFuncReturnValue = List[str] | Tuple[str, ...] | str | InstallArgs | None
PackagesFuncReturnValue = InstallArgsFuncReturnValue
InstallFuncReturnValue = str | None
ActionFuncReturnValue = str | bool | None
ProviderFuncReturnValue = AbspathFuncReturnValue | VersionFuncReturnValue | InstallArgsFuncReturnValue | InstallFuncReturnValue | ActionFuncReturnValue

@runtime_checkable
class AbspathFuncWithArgs(Protocol):
    def __call__(_self, binprovider: 'BinProvider', bin_name: BinName, **context) -> 'AbspathFuncReturnValue':
        ...

@runtime_checkable
class VersionFuncWithArgs(Protocol):
    def __call__(_self, binprovider: 'BinProvider', bin_name: BinName, **context) -> 'VersionFuncReturnValue':
        ...
        
@runtime_checkable
class InstallArgsFuncWithArgs(Protocol):
    def __call__(_self, binprovider: 'BinProvider', bin_name: BinName, **context) -> 'InstallArgsFuncReturnValue':
        ...

PackagesFuncWithArgs = InstallArgsFuncWithArgs

@runtime_checkable
class InstallFuncWithArgs(Protocol):
    def __call__(_self, binprovider: 'BinProvider', bin_name: BinName, **context) -> 'InstallFuncReturnValue':
        ...

@runtime_checkable
class ActionFuncWithArgs(Protocol):
    def __call__(_self, binprovider: 'BinProvider', bin_name: BinName, **context) -> 'ActionFuncReturnValue':
        ...

AbspathFuncWithNoArgs = Callable[[], AbspathFuncReturnValue]
VersionFuncWithNoArgs = Callable[[], VersionFuncReturnValue]
InstallArgsFuncWithNoArgs = Callable[[], InstallArgsFuncReturnValue]
PackagesFuncWithNoArgs = InstallArgsFuncWithNoArgs
InstallFuncWithNoArgs = Callable[[], InstallFuncReturnValue]
ActionFuncWithNoArgs = Callable[[], ActionFuncReturnValue]

AbspathHandlerValue = SelfMethodName | AbspathFuncWithNoArgs | AbspathFuncWithArgs | AbspathFuncReturnValue
VersionHandlerValue = SelfMethodName | VersionFuncWithNoArgs | VersionFuncWithArgs | VersionFuncReturnValue
InstallArgsHandlerValue = SelfMethodName | InstallArgsFuncWithNoArgs | InstallArgsFuncWithArgs | InstallArgsFuncReturnValue
PackagesHandlerValue = InstallArgsHandlerValue
InstallHandlerValue = SelfMethodName | InstallFuncWithNoArgs | InstallFuncWithArgs | InstallFuncReturnValue
ActionHandlerValue = SelfMethodName | ActionFuncWithNoArgs | ActionFuncWithArgs | ActionFuncReturnValue

HandlerType = Literal['abspath', 'version', 'install_args', 'packages', 'install', 'update', 'uninstall']
HandlerValue = AbspathHandlerValue | VersionHandlerValue | InstallArgsHandlerValue | InstallHandlerValue | ActionHandlerValue
HandlerReturnValue = AbspathFuncReturnValue | VersionFuncReturnValue | InstallArgsFuncReturnValue | InstallFuncReturnValue | ActionFuncReturnValue

class HandlerDict(TypedDict, total=False):
    abspath: AbspathHandlerValue
    version: VersionHandlerValue
    install_args: InstallArgsHandlerValue
    packages: InstallArgsHandlerValue
    install: InstallHandlerValue
    update: ActionHandlerValue
    uninstall: ActionHandlerValue

# Binary.overrides map BinProviderName:HandlerType:Handler    {'brew': {'install_args': [...]}}
BinaryOverrides = Dict[BinProviderName, HandlerDict]

# BinProvider.overrides map BinName:HandlerType:Handler       {'wget': {'install_args': [...]}}
BinProviderOverrides = Dict[BinName | Literal['*'], HandlerDict]

# Resolve forward refs at import time so downstream subclasses don't need to call model_rebuild().
ShallowBinary.model_rebuild(_types_namespace=globals())
BinProvider.model_rebuild(_types_namespace=globals())
EnvProvider.model_rebuild(_types_namespace=globals())
