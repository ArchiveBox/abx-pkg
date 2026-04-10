#!/usr/bin/env python

__package__ = "abx_pkg"

import os
import sys
import site
import re
import shutil
import sysconfig
import tempfile
from platformdirs import user_cache_path

from pathlib import Path
from typing import Self
from pydantic import Field, model_validator, TypeAdapter, computed_field

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    HostBinPath,
    abx_pkg_install_root_default,
    bin_abspath,
    bin_abspaths,
)
from .semver import SemVer
from .binprovider import (
    BinProvider,
    DEFAULT_ENV_PATH,
    env_flag_is_true,
    remap_kwargs,
)
from .logging import format_subprocess_output

ACTIVE_VENV = os.getenv("VIRTUAL_ENV", None)
_CACHED_GLOBAL_PIP_BIN_DIRS: set[str] | None = None


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "pip-cache"
try:
    pip_user_cache_path = user_cache_path(
        appname="pip",
        appauthor="abx-pkg",
        ensure_exists=True,
    )
    if os.access(pip_user_cache_path, os.W_OK):
        USER_CACHE_PATH = pip_user_cache_path
except Exception:
    pass


# pip >= 26.0 is required for ``--uploaded-prior-to`` (see pypa/pip#13625).
_PIP_MIN_RELEASE_AGE_VERSION = SemVer((26, 0, 0))


class PipProvider(BinProvider):
    name: BinProviderName = "pip"
    INSTALLER_BIN: BinName = "pip"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABX_PKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    # None = system site-packages (user or global), otherwise a path.
    # Default: ABX_PKG_PIP_ROOT > ABX_PKG_LIB_DIR/pip > None.
    install_root: Path | None = Field(
        default_factory=lambda: abx_pkg_install_root_default("pip"),
        validation_alias="pip_venv",
    )
    bin_dir: Path | None = None

    cache_dir: Path = USER_CACHE_PATH
    cache_arg: str = f"--cache-dir={cache_dir}"

    pip_install_args: list[str] = [
        "--no-input",
        "--disable-pip-version-check",
        "--quiet",
    ]  # extra args for pip install ... e.g. --upgrade
    pip_bootstrap_packages: list[str] = [
        "pip",
        "setuptools",
    ]  # packages installed into newly created install_root environments

    _INSTALLER_BIN_ABSPATH: HostBinPath | None = (
        None  # speed optimization only, faster to cache the abspath than to recompute it on every access
    )

    def supports_min_release_age(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        # When ``install_root`` is set, ``setup()`` bootstraps the venv with the
        # latest pip from PyPI (via ``pip install --upgrade pip``), so we can
        # assume ``--uploaded-prior-to`` support regardless of what the host
        # system pip looks like right now. This check runs before ``setup()``
        # on the first install, so inspecting ``INSTALLER_BINARY`` here would
        # otherwise see ``None``.
        if self.install_root and "pip" in self.pip_bootstrap_packages:
            return True
        installer = self.INSTALLER_BINARY
        version = installer.loaded_version if installer else None
        if version is None:
            return False
        return version >= _PIP_MIN_RELEASE_AGE_VERSION  # pyright: ignore[reportOperatorIssue]

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    @staticmethod
    def _install_args_have_option(args: InstallArgs, *options: str) -> bool:
        return any(
            arg == option or arg.startswith(f"{option}=")
            for arg in args
            for option in options
        )

    @computed_field
    @property
    def is_valid(self) -> bool:
        """False if install_root is not created yet or if pip binary is not found in PATH"""
        if self.install_root:
            venv_pip_path = self.install_root / "bin" / "python"
            venv_pip_binary_exists = os.path.isfile(venv_pip_path) and os.access(
                venv_pip_path,
                os.X_OK,
            )
            if not venv_pip_binary_exists:
                return False

        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Actual absolute path of the underlying package manager (e.g. /usr/local/bin/pip)"""
        if self._INSTALLER_BIN_ABSPATH:
            # return cached value if we have one
            return self._INSTALLER_BIN_ABSPATH

        abspath = None
        pip_binary = os.getenv("PIP_BINARY")

        if pip_binary and Path(pip_binary).expanduser().is_absolute():
            abspath = Path(pip_binary).expanduser()
        elif self.install_root:
            # use venv pip
            venv_pip_path = self.install_root / "bin" / self.INSTALLER_BIN
            if (
                os.path.isfile(venv_pip_path)
                and os.access(venv_pip_path, os.R_OK)
                and os.access(venv_pip_path, os.X_OK)
            ):
                abspath = str(venv_pip_path)
        else:
            # use system pip
            relpath = bin_abspath(
                self.INSTALLER_BIN,
                PATH=DEFAULT_ENV_PATH,
            ) or shutil.which(self.INSTALLER_BIN)
            abspath = (
                relpath and Path(relpath).resolve()
            )  # find self.INSTALLER_BIN abspath using environment path

        if not abspath:
            # underlying package manager not found on this host, return None
            return None
        valid_abspath = TypeAdapter(HostBinPath).validate_python(abspath)
        if valid_abspath:
            # if we found a valid abspath, cache it
            self._INSTALLER_BIN_ABSPATH = valid_abspath
        return valid_abspath

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Detect the user (UID) to run as when executing pip."""
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "bin"

        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.install_root,),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_pip_sitepackages(self) -> Self:
        """Assemble PATH from install_root or autodetected global python system site-packages and user site-packages"""
        global _CACHED_GLOBAL_PIP_BIN_DIRS
        PATH = self.PATH

        pip_bin_dirs = set()

        if self.bin_dir:
            self.PATH = self._merge_PATH(self.bin_dir)
            return self
        else:
            # autodetect global system python paths

            if _CACHED_GLOBAL_PIP_BIN_DIRS:
                pip_bin_dirs = _CACHED_GLOBAL_PIP_BIN_DIRS.copy()
            else:
                pip_bin_dirs = {
                    *(
                        str(
                            Path(sitepackage_dir).parent.parent.parent / "bin",
                        )  # /opt/homebrew/opt/python@3.11/Frameworks/Python.framework/Versions/3.11/bin
                        for sitepackage_dir in site.getsitepackages()
                    ),
                    str(
                        Path(site.getusersitepackages()).parent.parent.parent / "bin",
                    ),  # /Users/squash/Library/Python/3.9/bin
                    sysconfig.get_path("scripts"),  # /opt/homebrew/bin
                    str(
                        Path(sys.executable).resolve().parent,
                    ),  # /opt/homebrew/Cellar/python@3.11/3.11.9/Frameworks/Python.framework/Versions/3.11/bin
                }

                # find every python installed in the system PATH and add their parent path, as that's where its corresponding pip will link global bins
                for abspath in bin_abspaths(
                    "python",
                    PATH=DEFAULT_ENV_PATH,
                ):  # ~/Library/Frameworks/Python.framework/Versions/3.10/bin
                    pip_bin_dirs.add(str(abspath.parent))
                for abspath in bin_abspaths(
                    "python3",
                    PATH=DEFAULT_ENV_PATH,
                ):  # /usr/local/bin or anywhere else we see python3 in $PATH
                    pip_bin_dirs.add(str(abspath.parent))

                _CACHED_GLOBAL_PIP_BIN_DIRS = pip_bin_dirs.copy()

            # remove any active venv from PATH because we're trying to only get the global system python paths
            if ACTIVE_VENV:
                pip_bin_dirs.discard(f"{ACTIVE_VENV}/bin")

        self.PATH = self._merge_PATH(*sorted(pip_bin_dirs), PATH=PATH)
        return self

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        """create pip venv dir if needed"""
        if not self._ensure_writable_cache_dir(self.cache_dir):
            self.cache_arg = "--no-cache-dir"

        if self.install_root:
            self._setup_venv(self.install_root, no_cache=no_cache)

    def _setup_venv(self, pip_venv: Path, *, no_cache: bool = False) -> None:
        pip_venv.parent.mkdir(parents=True, exist_ok=True)

        # create new venv in pip_venv if it doesn't exist
        venv_pip_path = pip_venv / "bin" / "python"
        venv_pip_binary_exists = os.path.isfile(venv_pip_path) and os.access(
            venv_pip_path,
            os.X_OK,
        )
        if venv_pip_binary_exists:
            return

        import venv

        venv.create(
            str(pip_venv),
            system_site_packages=False,
            clear=True,
            symlinks=True,
            with_pip=True,
            upgrade_deps=True,
        )
        assert os.path.isfile(venv_pip_path) and os.access(
            venv_pip_path,
            os.X_OK,
        ), f"could not find pip inside venv after creating it: {pip_venv}"

        # Bootstrap pip + setuptools into the newly created venv. We skip
        # security flags here because the venv was just created by Python's
        # own ``venv`` module and we're upgrading its baseline tooling.
        pip_abspath = self._require_installer_bin()
        proc = self.exec(
            bin_name=pip_abspath,
            cmd=[
                "install",
                "--no-cache-dir" if no_cache else self.cache_arg,
                "--no-input",
                "--disable-pip-version-check",
                "--quiet",
                "--upgrade",
                *self.pip_bootstrap_packages,
            ],
            quiet=True,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", self.pip_bootstrap_packages, proc)

    def _security_flags(
        self,
        install_args: InstallArgs,
        *,
        postinstall_scripts: bool,
        min_release_age: float,
    ) -> list[str]:
        """Build pip ``install`` security flags based on provider config.

        - ``--only-binary :all:`` when ``postinstall_scripts`` is disabled
          (wheels only, no sdist builds — pip's equivalent of ``--no-build``).
        - ``--uploaded-prior-to=<ISO8601>`` when ``min_release_age`` is set
          and pip is new enough to support the flag (pip >= 26.0, see
          pypa/pip#13625). Older pip versions silently skip the flag.
        """
        flags: list[str] = []

        has_only_binary_flag = self._install_args_have_option(
            install_args,
            "--only-binary",
        )
        if not postinstall_scripts and not has_only_binary_flag:
            flags.extend(["--only-binary", ":all:"])

        if min_release_age <= 0:
            return flags

        has_release_age_flag = self._install_args_have_option(
            install_args,
            "--uploaded-prior-to",
        )
        if has_release_age_flag:
            return flags

        installer = self.INSTALLER_BINARY
        pip_ver = installer.loaded_version if installer else None
        if pip_ver is None or pip_ver == SemVer((999, 999, 999)):
            return flags
        if pip_ver < _PIP_MIN_RELEASE_AGE_VERSION:  # pyright: ignore[reportOperatorIssue]
            return flags

        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=min_release_age)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        flags.append(f"--uploaded-prior-to={cutoff}")
        return flags

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        if self.install_root:
            self.setup(no_cache=no_cache)

        pip_abspath = self._require_installer_bin()
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}>={min_version}"
                if arg
                and not arg.startswith("-")
                and not any(c in arg for c in ">=<!=~")
                else arg
                for arg in install_args
            ]
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        min_release_age = 7.0 if min_release_age is None else min_release_age

        security_flags = self._security_flags(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        cache_arg = "--no-cache-dir" if no_cache else self.cache_arg

        proc = self.exec(
            bin_name=pip_abspath,
            cmd=[
                "install",
                cache_arg,
                *self.pip_install_args,
                *security_flags,
                *install_args,
            ],
            timeout=timeout,
        )

        if proc.returncode != 0:
            self._raise_proc_error("install", install_args, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        if self.install_root:
            self.setup(no_cache=no_cache)

        pip_abspath = self._require_installer_bin()
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}>={min_version}"
                if arg
                and not arg.startswith("-")
                and not any(c in arg for c in ">=<!=~")
                else arg
                for arg in install_args
            ]
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        min_release_age = 7.0 if min_release_age is None else min_release_age

        security_flags = self._security_flags(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )

        cache_arg = "--no-cache-dir" if no_cache else self.cache_arg
        proc = self.exec(
            bin_name=pip_abspath,
            cmd=[
                "install",
                cache_arg,
                *self.pip_install_args,
                "--upgrade",
                *security_flags,
                *install_args,
            ],
            timeout=timeout,
        )

        if proc.returncode != 0:
            self._raise_proc_error("update", install_args, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        timeout: int | None = None,
    ) -> bool:
        pip_abspath = self._require_installer_bin()
        install_args = install_args or self.get_install_args(bin_name)

        proc = self.exec(
            bin_name=pip_abspath,
            cmd=[
                "uninstall",
                "--yes",
                *install_args,
            ],
            timeout=timeout,
        )

        if proc.returncode != 0:
            self._raise_proc_error("uninstall", install_args, proc)

        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        **context,
    ) -> HostBinPath | None:

        # try searching for the bin_name in BinProvider.PATH first (fastest)
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except ValueError:
            pass

        pip_abspath = self.INSTALLER_BIN_ABSPATH
        if not pip_abspath:
            return None

        # fallback to using pip show to get the site-packages bin path
        output_lines = (
            self.exec(
                bin_name=pip_abspath,
                cmd=["show", "--no-input", str(bin_name)],
                quiet=False,
                timeout=self.version_timeout,
            )
            .stdout.strip()
            .split("\n")
        )
        # For more information, please refer to <http://unlicense.org/>
        # Location: /Volumes/NVME/Users/squash/Library/Python/3.11/lib/python/site-packages
        # Requires: brotli, certifi, mutagen, pycryptodomex, requests, urllib3, websockets
        # Required-by:
        try:
            location = [line for line in output_lines if line.startswith("Location: ")][
                0
            ].split("Location: ", 1)[-1]
        except IndexError:
            return None
        PATH = str(Path(location).parent.parent.parent / "bin")
        abspath = bin_abspath(str(bin_name), PATH=PATH)
        if abspath:
            return TypeAdapter(HostBinPath).validate_python(abspath)
        else:
            return None

    @staticmethod
    def _package_name_from_install_arg(install_arg: str) -> str | None:
        if not install_arg or install_arg.startswith("-"):
            return None
        if "://" in install_arg:
            return None
        if install_arg.startswith((".", "/", "~")):
            return None
        package_name = re.split(r"[<>=!~;]", install_arg, maxsplit=1)[0]
        package_name = package_name.split("[", 1)[0].strip()
        return package_name or None

    def _package_name_for_bin(self, bin_name: BinName) -> str | None:
        install_args = self.get_install_args(bin_name, quiet=True)
        for install_arg in install_args:
            package_name = self._package_name_from_install_arg(install_arg)
            if package_name:
                return package_name
        return None

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        **context,
    ) -> SemVer | None:
        try:
            version = self._version_from_exec(
                bin_name,
                abspath=abspath,
                timeout=timeout,
            )
            if version:
                return version
        except ValueError:
            pass

        pip_abspath = self.INSTALLER_BIN_ABSPATH
        if not pip_abspath:
            return None

        # fallback to using pip show to get the version (slower)
        package_name = self._package_name_for_bin(bin_name) or str(bin_name)
        output_lines = (
            self.exec(
                bin_name=pip_abspath,
                cmd=["show", "--no-input", package_name],
                quiet=False,
                timeout=timeout,
            )
            .stdout.strip()
            .split("\n")
        )
        try:
            version_str = [
                line for line in output_lines if line.startswith("Version: ")
            ][0].split("Version: ", 1)[-1]
            return SemVer.parse(version_str)
        except Exception:
            return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_pip.py load yt-dlp
    # ./binprovider_pip.py install pip
    # ./binprovider_pip.py get_version pip
    # ./binprovider_pip.py get_abspath pip
    result = pip = PipProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(pip, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)
