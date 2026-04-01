#!/usr/bin/env python3
__package__ = "abx_pkg"

import os
import sys
import time
import platform
from pathlib import Path

from pydantic import model_validator, TypeAdapter

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    HostBinPath,
    bin_abspath,
)
from .semver import SemVer
from .binprovider import BinProvider, remap_kwargs, env_flag_is_true
from .logging import format_subprocess_output, get_logger, log_subprocess_error

logger = get_logger(__name__)

OS = platform.system().lower()

NEW_MACOS_DIR = Path("/opt/homebrew/bin")
OLD_MACOS_DIR = Path("/usr/local/bin")
DEFAULT_MACOS_DIR = NEW_MACOS_DIR if platform.machine() == "arm64" else OLD_MACOS_DIR
DEFAULT_LINUX_DIR = Path("/home/linuxbrew/.linuxbrew/bin")
GUESSED_BREW_PREFIX = (
    DEFAULT_MACOS_DIR.parent if OS == "darwin" else DEFAULT_LINUX_DIR.parent
)

_LAST_UPDATE_CHECK = None
UPDATE_CHECK_INTERVAL = 60 * 60 * 24  # 1 day


class BrewProvider(BinProvider):
    name: BinProviderName = "brew"
    INSTALLER_BIN: BinName = "brew"

    PATH: PATHStr = f"{DEFAULT_LINUX_DIR}:{NEW_MACOS_DIR}:{OLD_MACOS_DIR}"

    brew_prefix: Path = GUESSED_BREW_PREFIX

    def _brew_prefixes(self) -> list[Path]:
        prefixes: list[Path] = []
        seen: set[str] = set()

        def add_prefix(bin_dir_or_prefix: Path) -> None:
            prefix = (
                bin_dir_or_prefix.parent
                if bin_dir_or_prefix.name == "bin"
                else bin_dir_or_prefix
            )
            prefix_str = str(prefix)
            if prefix_str in seen:
                return
            seen.add(prefix_str)
            prefixes.append(prefix)

        installer_bin = self.INSTALLER_BIN_ABSPATH
        if installer_bin:
            add_prefix(Path(installer_bin).parent)

        for bin_dir in self.PATH.split(":"):
            if not bin_dir:
                continue
            add_prefix(Path(bin_dir))

        return prefixes

    def _brew_search_paths(self, bin_name: BinName | HostBinPath) -> PATHStr:
        package_names = [
            package
            for package in self.get_install_args(str(bin_name), quiet=True)
            if isinstance(package, str) and package and not package.startswith("-")
        ] or [str(bin_name)]

        search_paths: list[str] = []
        seen: set[str] = set()

        def add_path(path: Path) -> None:
            path_str = str(path)
            if path_str in seen:
                return
            seen.add(path_str)
            search_paths.append(path_str)

        for prefix in self._brew_prefixes():
            for package in package_names:
                add_path(prefix / "opt" / package / "bin")
                add_path(prefix / "opt" / package / "libexec" / "bin")
                for cellar_bin in (prefix / "Cellar" / package).glob("*/bin"):
                    add_path(cellar_bin)
                for cellar_bin in (prefix / "Cellar" / package).glob("*/libexec/bin"):
                    add_path(cellar_bin)

        for bin_dir in self.PATH.split(":"):
            if bin_dir:
                add_path(Path(bin_dir))

        return TypeAdapter(PATHStr).validate_python(search_paths)

    @model_validator(mode="after")
    def load_PATH(self):
        if not self.INSTALLER_BIN_ABSPATH:
            # brew is not available on this host
            self.PATH: PATHStr = ""
            return self

        bin_dirs: list[str] = []
        seen: set[str] = set()

        def add_bin_dir(path: Path) -> None:
            path_str = str(path)
            if path_str in seen:
                return
            seen.add(path_str)
            bin_dirs.append(path_str)

        add_bin_dir(Path(self.INSTALLER_BIN_ABSPATH).parent)

        if OS == "darwin":
            for path in (DEFAULT_MACOS_DIR, NEW_MACOS_DIR, OLD_MACOS_DIR):
                if os.path.isdir(path) and os.access(path, os.R_OK):
                    add_bin_dir(path)
        else:
            if os.path.isdir(DEFAULT_LINUX_DIR) and os.access(
                DEFAULT_LINUX_DIR,
                os.R_OK,
            ):
                add_bin_dir(DEFAULT_LINUX_DIR)

        self.brew_prefix = self._brew_prefixes()[0]
        self.PATH = TypeAdapter(PATHStr).validate_python(bin_dirs)
        return self

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> str:
        global _LAST_UPDATE_CHECK

        install_args = install_args or self.get_install_args(bin_name)

        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}",
            )

        # print(f'[*] {self.__class__.__name__}: Installing {bin_name}: {self.INSTALLER_BIN_ABSPATH} install {install_args}')

        # Attempt 1: Try installing with Pyinfra
        from .binprovider_pyinfra import PYINFRA_INSTALLED, pyinfra_package_install

        if PYINFRA_INSTALLED:
            return pyinfra_package_install(
                (bin_name,),
                installer_module="operations.brew.packages",
            )

        # Attempt 2: Try installing with Ansible
        from .binprovider_ansible import ANSIBLE_INSTALLED, ansible_package_install

        if ANSIBLE_INSTALLED:
            return ansible_package_install(
                bin_name,
                installer_module="community.general.homebrew",
            )

        # Attempt 3: Fallback to installing manually by calling brew in shell

        if (
            not _LAST_UPDATE_CHECK
            or (time.time() - _LAST_UPDATE_CHECK) > UPDATE_CHECK_INTERVAL
        ):
            # only update if we haven't checked in the last day
            self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=["update"])
            _LAST_UPDATE_CHECK = time.time()

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=[
                "install",
                *(
                    ["--skip-post-install"]
                    if not env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS")
                    else []
                ),
                *install_args,
            ],
        )
        if proc.returncode != 0:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} install",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__} install got returncode {proc.returncode} while installing {install_args}: {install_args}\n{format_subprocess_output(proc.stdout, proc.stderr)}".strip(),
            )

        return proc.stderr.strip() + "\n" + proc.stdout.strip()

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> str:
        global _LAST_UPDATE_CHECK

        install_args = install_args or self.get_install_args(bin_name)

        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}",
            )

        from .binprovider_pyinfra import PYINFRA_INSTALLED, pyinfra_package_install

        if PYINFRA_INSTALLED:
            return pyinfra_package_install(
                install_args,
                installer_module="operations.brew.packages",
                installer_extra_kwargs={"latest": True},
            )

        from .binprovider_ansible import ANSIBLE_INSTALLED, ansible_package_install

        if ANSIBLE_INSTALLED:
            return ansible_package_install(
                install_args,
                installer_module="community.general.homebrew",
                state="latest",
            )

        if (
            not _LAST_UPDATE_CHECK
            or (time.time() - _LAST_UPDATE_CHECK) > UPDATE_CHECK_INTERVAL
        ):
            self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=["update"])
            _LAST_UPDATE_CHECK = time.time()

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=[
                "upgrade",
                *(
                    ["--skip-post-install"]
                    if not env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS")
                    else []
                ),
                *install_args,
            ],
        )
        if proc.returncode != 0:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} update",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__} update got returncode {proc.returncode} while updating {install_args}: {install_args}\n{format_subprocess_output(proc.stdout, proc.stderr)}".strip(),
            )

        return proc.stderr.strip() + "\n" + proc.stdout.strip()

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> bool:
        install_args = install_args or self.get_install_args(bin_name)

        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}",
            )

        from .binprovider_pyinfra import PYINFRA_INSTALLED, pyinfra_package_install

        if PYINFRA_INSTALLED:
            pyinfra_package_install(
                install_args,
                installer_module="operations.brew.packages",
                installer_extra_kwargs={"present": False},
            )
            return True

        from .binprovider_ansible import ANSIBLE_INSTALLED, ansible_package_install

        if ANSIBLE_INSTALLED:
            ansible_package_install(
                install_args,
                installer_module="community.general.homebrew",
                state="absent",
            )
            return True

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["uninstall", *install_args],
        )
        if proc.returncode != 0:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} uninstall",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__} uninstall got returncode {proc.returncode} while uninstalling {install_args}: {install_args}\n{format_subprocess_output(proc.stdout, proc.stderr)}".strip(),
            )

        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        **context,
    ) -> HostBinPath | None:
        # print(f'[*] {self.__class__.__name__}: Getting abspath for {bin_name}...')

        if not self.PATH:
            return None

        search_paths = self._brew_search_paths(bin_name)
        abspath = bin_abspath(bin_name, PATH=search_paths)
        if abspath:
            return abspath

        if not self.INSTALLER_BIN_ABSPATH:
            return None

        for package in self.get_install_args(str(bin_name)) or [str(bin_name)]:
            try:
                paths = (
                    self.exec(
                        bin_name=self.INSTALLER_BIN_ABSPATH,
                        cmd=["list", "--formula", package],
                        timeout=self._version_timeout,
                        quiet=True,
                    )
                    .stdout.strip()
                    .split("\n")
                )
                for path_str in paths:
                    path = Path(path_str.strip())
                    if path.name != str(bin_name):
                        continue
                    if path.is_file() and os.access(path, os.X_OK):
                        return bin_abspath(path)
            except Exception:
                pass

        # This code works but there's no need, the method above is much faster:

        # # try checking filesystem or using brew list to get the Cellar bin path (faster than brew info)
        # for package in (self.get_install_args(str(bin_name)) or [str(bin_name)]):
        #     try:
        #         paths = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=[
        #             'list',
        #             '--formulae',
        #             package,
        #         ], timeout=self._version_timeout, quiet=True).stdout.strip().split('\n')
        #         # /opt/homebrew/Cellar/curl/8.10.1/bin/curl
        #         # /opt/homebrew/Cellar/curl/8.10.1/bin/curl-config
        #         # /opt/homebrew/Cellar/curl/8.10.1/include/curl/ (12 files)
        #         return [line for line in paths if '/Cellar/' in line and line.endswith(f'/bin/{bin_name}')][0].strip()
        #     except Exception:
        #         pass

        # # fallback to using brew info to get the Cellar bin path
        # for package in (self.get_install_args(str(bin_name)) or [str(bin_name)]):
        #     try:
        #         info_lines = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=[
        #             'info',
        #             '--quiet',
        #             package,
        #         ], timeout=self._version_timeout, quiet=True).stdout.strip().split('\n')
        #         # /opt/homebrew/Cellar/curl/8.10.0 (530 files, 4MB)
        #         cellar_path = [line for line in info_lines if '/Cellar/' in line][0].rsplit(' (', 1)[0]
        #         abspath = bin_abspath(bin_name, PATH=f'{cellar_path}/bin')
        #         if abspath:
        #             return abspath
        #     except Exception:
        #         pass
        # return None

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        **context,
    ) -> SemVer | None:
        # print(f'[*] {self.__class__.__name__}: Getting version for {bin_name}...')

        # shortcut: if we already have the Cellar abspath, extract the version from it
        if abspath and "/Cellar/" in str(abspath):
            # /opt/homebrew/Cellar/curl/8.10.1/bin/curl -> 8.10.1
            version = str(abspath).rsplit(f"/bin/{bin_name}", 1)[0].rsplit("/", 1)[-1]
            if version:
                try:
                    parsed = SemVer.parse(version)
                    if parsed:
                        return parsed
                except ValueError:
                    pass

        # fallback to running $ <bin_name> --version
        try:
            version = super().default_version_handler(
                bin_name,
                abspath=abspath,
                **context,
            )
            if version:
                return version if isinstance(version, SemVer) else SemVer.parse(version)
        except ValueError:
            pass

        if not self.INSTALLER_BIN_ABSPATH:
            return None

        # fallback to using brew list to get the package version (faster than brew info)
        for package in self.get_install_args(str(bin_name)) or [str(bin_name)]:
            try:
                paths = (
                    self.exec(
                        bin_name=self.INSTALLER_BIN_ABSPATH,
                        cmd=[
                            "list",
                            "--formulae",
                            package,
                        ],
                        timeout=self._version_timeout,
                        quiet=True,
                    )
                    .stdout.strip()
                    .split("\n")
                )
                # /opt/homebrew/Cellar/curl/8.10.1/bin/curl
                cellar_abspath = [
                    line
                    for line in paths
                    if "/Cellar/" in line and line.rstrip("/").endswith(f"/{bin_name}")
                ][0].strip()
                path = Path(cellar_abspath)
                if "Cellar" in path.parts:
                    cellar_idx = path.parts.index("Cellar")
                    if len(path.parts) > cellar_idx + 2:
                        version = path.parts[cellar_idx + 2]
                        if version:
                            return SemVer.parse(version)
            except Exception:
                pass

        # fallback to using brew info to get the version (slowest method of all)
        install_args = self.get_install_args(str(bin_name)) or [str(bin_name)]
        main_package = install_args[0]  # assume first package in list is the main one
        try:
            version_str = (
                self.exec(
                    bin_name=self.INSTALLER_BIN_ABSPATH,
                    cmd=[
                        "info",
                        "--quiet",
                        main_package,
                    ],
                    quiet=True,
                    timeout=self._version_timeout,
                )
                .stdout.strip()
                .split("\n")[0]
            )
            # ==> curl: stable 8.10.1 (bottled), HEAD [keg-only]
            return SemVer.parse(version_str)
        except Exception:
            return None

        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_brew.py load yt-dlp
    # ./binprovider_brew.py install pip
    # ./binprovider_brew.py get_version pip
    # ./binprovider_brew.py get_abspath pip
    result = brew = BrewProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(brew, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)
