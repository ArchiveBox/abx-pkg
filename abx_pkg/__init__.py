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
from .logging import (
    logger,
    get_logger,
    configure_logging,
    configure_rich_logging,
    RICH_INSTALLED,
)
from .exceptions import (
    ABXPkgError,
    BinaryOperationError,
    BinaryInstallError,
    BinaryLoadError,
    BinaryLoadOrInstallError,
    BinaryUpdateError,
    BinaryUninstallError,
)
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
from .binprovider_cargo import CargoProvider
from .binprovider_gem import GemProvider
from .binprovider_goget import GoGetProvider
from .binprovider_nix import NixProvider
from .binprovider_docker import DockerProvider
from .binprovider_pip import PipProvider
from .binprovider_npm import NpmProvider
from .binprovider_ansible import AnsibleProvider
from .binprovider_pyinfra import PyinfraProvider
from .binprovider_chromewebstore import ChromeWebstoreProvider
from .binprovider_puppeteer import PuppeteerProvider
from .binprovider_custom import CustomProvider

ALL_PROVIDERS = [
    EnvProvider,
    AptProvider,
    BrewProvider,
    CargoProvider,
    GemProvider,
    GoGetProvider,
    NixProvider,
    DockerProvider,
    PipProvider,
    NpmProvider,
    AnsibleProvider,
    PyinfraProvider,
    ChromeWebstoreProvider,
    PuppeteerProvider,
    CustomProvider,
]


def _provider_class(provider: type[BinProvider] | BinProvider) -> type[BinProvider]:
    return provider if isinstance(provider, type) else type(provider)


ALL_PROVIDER_NAMES = [
    _provider_class(provider).model_fields["name"].default for provider in ALL_PROVIDERS
]  # pip, apt, brew, etc.
ALL_PROVIDER_CLASS_NAMES = [
    _provider_class(provider).__name__ for provider in ALL_PROVIDERS
]  # PipProvider, AptProvider, BrewProvider, etc.

# Lazy provider singletons: maps provider name -> class
# e.g. 'apt' -> AptProvider, 'pip' -> PipProvider, 'env' -> EnvProvider
_PROVIDER_CLASS_BY_NAME = {
    _provider_class(provider).model_fields["name"].default: _provider_class(provider)
    for provider in ALL_PROVIDERS
}
_provider_singletons: dict = {}


def __getattr__(name: str):
    if name in _PROVIDER_CLASS_BY_NAME:
        if name not in _provider_singletons:
            _provider_singletons[name] = _PROVIDER_CLASS_BY_NAME[name]()
        return _provider_singletons[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Main types
    "BinProvider",
    "Binary",
    "SemVer",
    "ShallowBinary",
    "logger",
    "get_logger",
    "configure_logging",
    "configure_rich_logging",
    "RICH_INSTALLED",
    # Exceptions
    "ABXPkgError",
    "BinaryOperationError",
    "BinaryInstallError",
    "BinaryLoadError",
    "BinaryLoadOrInstallError",
    "BinaryUpdateError",
    "BinaryUninstallError",
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
    # BinProviders (classes)
    "EnvProvider",
    "AptProvider",
    "BrewProvider",
    "CargoProvider",
    "GemProvider",
    "GoGetProvider",
    "NixProvider",
    "DockerProvider",
    "PipProvider",
    "NpmProvider",
    "AnsibleProvider",
    "PyinfraProvider",
    "ChromeWebstoreProvider",
    "PuppeteerProvider",
    "CustomProvider",
    # Note: provider singleton names (apt, pip, brew, etc.) are intentionally
    # excluded from __all__ so that `from abx_pkg import *` does not eagerly
    # instantiate every provider. Use explicit imports instead:
    #   from abx_pkg import apt, pip, brew
]
