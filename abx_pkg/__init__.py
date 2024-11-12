__package__ = "abx_pkg"

from .base_types import (
    BinName,
    InstallArgs,
    PATHStr,
    HostBinPath,
    HostExistsPath,
    BinDirPath,
    BinProviderName,
    bin_name,
    bin_abspath,
    bin_abspaths,
    func_takes_args_or_kwargs,
)
from .semver import SemVer, bin_version
from .shallowbinary import ShallowBinary
from .binprovider import (
    BinProvider,
    EnvProvider,
    OPERATING_SYSTEM,
    DEFAULT_PATH,
    DEFAULT_ENV_PATH,
    PYTHON_BIN_DIR,
    BinProviderOverrides,
    BinaryOverrides,
    ProviderFuncReturnValue,
    HandlerType,
    HandlerValue,
    HandlerDict,
    HandlerReturnValue,
)
from .binary import Binary

from .binprovider_apt import AptProvider
from .binprovider_brew import BrewProvider
from .binprovider_pip import PipProvider
from .binprovider_npm import NpmProvider
from .binprovider_ansible import AnsibleProvider
from .binprovider_pyinfra import PyinfraProvider

ALL_PROVIDERS = [
    EnvProvider,
    AptProvider,
    BrewProvider,
    PipProvider,
    NpmProvider,
    AnsibleProvider,
    PyinfraProvider,
]
ALL_PROVIDER_NAMES = [provider.__fields__['name'].default for provider in ALL_PROVIDERS]
ALL_PROVIDER_CLASSES = [provider.__class__.__name__ for provider in ALL_PROVIDERS]


__all__ = [
    # Main types
    "BinProvider",
    "Binary",
    "SemVer",
    "ShallowBinary",
    
    # Helper Types
    "BinName",
    "InstallArgs",
    "PATHStr",
    "BinDirPath",
    "HostBinPath",
    "HostExistsPath",
    "BinProviderName",
    
    # Override types
    "BinProviderOverrides",
    "BinaryOverrides",
    "ProviderFuncReturnValue",
    "HandlerType",
    "HandlerValue",
    "HandlerDict",
    "HandlerReturnValue",
    
    # Validator Functions
    "bin_version",
    "bin_name",
    "bin_abspath",
    "bin_abspaths",
    "func_takes_args_or_kwargs",
    
    # Globals
    "OPERATING_SYSTEM",
    "DEFAULT_PATH",
    "DEFAULT_ENV_PATH",
    "PYTHON_BIN_DIR",
    
    # BinProviders
    *ALL_PROVIDER_CLASSES,
]
