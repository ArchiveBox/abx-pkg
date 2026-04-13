__package__ = "abxpkg"

from collections.abc import Mapping


class ABXPkgError(Exception):
    pass


class BinProviderError(ABXPkgError):
    pass


class BinProviderOperationError(BinProviderError):
    action: str = "operate on"

    def __init__(
        self,
        provider_name: str,
        target: object,
        returncode: int | None = None,
        output: str | None = None,
    ):
        self.provider_name = provider_name
        self.target = target
        self.returncode = returncode
        self.output = (output or "").strip()
        message = f"{self.provider_name} {self.action} failed for {self.target!r}"
        if self.returncode is not None:
            message += f" with returncode {self.returncode}"
        if self.output:
            message += f"\n{self.output}"
        super().__init__(message)


class BinProviderInstallError(BinProviderOperationError):
    action = "install"


class BinProviderUpdateError(BinProviderOperationError):
    action = "update"


class BinProviderUninstallError(BinProviderOperationError):
    action = "uninstall"


class BinProviderUnavailableError(BinProviderError):
    def __init__(self, provider_name: str, installer_bin: str):
        self.provider_name = provider_name
        self.installer_bin = installer_bin
        super().__init__(
            f"{self.provider_name} is disabled because {self.installer_bin} is not available on this host",
        )


class BinaryOperationError(ABXPkgError):
    action: str = "operate on"

    def __init__(
        self,
        binary_name: str,
        provider_names: str,
        errors: Mapping[str, str] | None = None,
    ):
        self.binary_name = binary_name
        self.provider_names = provider_names
        self.errors = dict(errors or {})
        super().__init__(
            f"Unable to {self.action} binary {self.binary_name} via providers {self.provider_names}. "
            f"ERRORS={self.errors}",
        )


class BinaryInstallError(BinaryOperationError):
    action = "install"


class BinaryLoadError(BinaryOperationError):
    action = "load"


class BinaryUpdateError(BinaryOperationError):
    action = "update"


class BinaryUninstallError(BinaryOperationError):
    action = "uninstall"
