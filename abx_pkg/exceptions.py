__package__ = "abx_pkg"

from typing import Mapping


class ABXPkgError(Exception):
    pass


class BinProviderError(ABXPkgError):
    pass


class BinProviderInstallError(BinProviderError):
    pass


class BinProviderUpdateError(BinProviderError):
    pass


class BinProviderUninstallError(BinProviderError):
    pass


class BinProviderUnavailableError(BinProviderError):
    pass


class BinaryOperationError(ABXPkgError):
    action: str = "operate on"

    def __init__(self, binary_name: str, provider_names: str, errors: Mapping[str, str] | None = None):
        self.binary_name = binary_name
        self.provider_names = provider_names
        self.errors = dict(errors or {})
        super().__init__(
            f"Unable to {self.action} binary {self.binary_name} via providers {self.provider_names}. "
            f"ERRORS={self.errors}"
        )


class BinaryInstallError(BinaryOperationError):
    action = "install"


class BinaryLoadError(BinaryOperationError):
    action = "load"


class BinaryLoadOrInstallError(BinaryOperationError):
    action = "load or install"


class BinaryUpdateError(BinaryOperationError):
    action = "update"


class BinaryUninstallError(BinaryOperationError):
    action = "uninstall"
