#!/usr/bin/env python

__package__ = "abx_pkg"

import os
import sys
import site
import shlex
import shutil
import sysconfig
import subprocess
import tempfile
from platformdirs import user_cache_path

from pathlib import Path
from typing import ClassVar, Self
from pydantic import model_validator, TypeAdapter, computed_field

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    HostBinPath,
    bin_abspath,
    bin_abspaths,
)
from .semver import SemVer
from .binprovider import (
    BinProvider,
    DEFAULT_ENV_PATH,
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


class PipProvider(BinProvider):
    name: BinProviderName = "pip"
    INSTALLER_BIN: BinName = "pip"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "pip_venv"

    PATH: PATHStr = ""

    pip_venv: Path | None = (
        None  # None = system site-packages (user or global), otherwise it's a path e.g. DATA_DIR/lib/pip/venv
    )

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
        "uv",
    ]  # packages installed into newly created pip_venv environments

    _INSTALLER_BIN_ABSPATH: HostBinPath | None = (
        None  # speed optimization only, faster to cache the abspath than to recompute it on every access
    )

    def supports_min_release_age(self, action) -> bool:
        return action in ("install", "update")

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
        """False if pip_venv is not created yet or if pip binary is not found in PATH"""
        if self.pip_venv:
            venv_pip_path = self.pip_venv / "bin" / "python"
            venv_pip_binary_exists = os.path.isfile(venv_pip_path) and os.access(
                venv_pip_path,
                os.X_OK,
            )
            if not venv_pip_binary_exists:
                return False

        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.pip_venv

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        return self.install_root / "bin" if self.install_root else None

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Actual absolute path of the underlying package manager (e.g. /usr/local/bin/npm)"""
        if self._INSTALLER_BIN_ABSPATH:
            # return cached value if we have one
            return self._INSTALLER_BIN_ABSPATH

        abspath = None
        pip_binary = os.getenv("PIP_BINARY")

        if pip_binary and Path(pip_binary).expanduser().is_absolute():
            abspath = Path(pip_binary).expanduser()
        elif self.pip_venv:
            # use venv pip
            venv_pip_path = self.pip_venv / "bin" / self.INSTALLER_BIN
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

        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.pip_venv,),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_pip_sitepackages(self) -> Self:
        """Assemble PATH from pip_venv or autodetected global python system site-packages and user site-packages"""
        global _CACHED_GLOBAL_PIP_BIN_DIRS
        PATH = self.PATH

        pip_bin_dirs = set()

        if self.pip_venv:
            self.PATH = self._merge_PATH(self.pip_venv / "bin")
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
    ):
        """create pip venv dir if needed"""
        postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        if not self._ensure_writable_cache_dir(self.cache_dir):
            self.cache_arg = "--no-cache-dir"

        if self.pip_venv:
            self._pip_setup_venv(
                self.pip_venv,
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
            )

    def _pip_setup_venv(
        self,
        pip_venv: Path,
        postinstall_scripts: bool = False,
        min_release_age: float = 7.0,
    ):
        pip_venv.parent.mkdir(parents=True, exist_ok=True)

        # create new venv in pip_venv if it doesn't exist
        venv_pip_path = pip_venv / "bin" / "python"
        venv_pip_binary_exists = os.path.isfile(venv_pip_path) and os.access(
            venv_pip_path,
            os.X_OK,
        )
        if not venv_pip_binary_exists:
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
            proc = self._pip(
                [
                    "install",
                    self.cache_arg,
                    "--upgrade",
                    *self.pip_bootstrap_packages,
                ],
                quiet=True,
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
            )  # setuptools is not installed by default after python >= 3.12, and uv is needed for fast pip-compatible installs
            if proc.returncode != 0:
                self._raise_proc_error("install", self.pip_bootstrap_packages, proc)

    def _uv_pip_target_args(self) -> list[str]:
        if self.pip_venv:
            return ["--python", str(self.pip_venv / "bin" / "python")]
        pip_abspath = self.INSTALLER_BIN_ABSPATH
        if pip_abspath:
            try:
                shebang = Path(pip_abspath).read_text(errors="ignore").splitlines()[0]
                if shebang.startswith("#!"):
                    command = shlex.split(shebang[2:].strip())
                    python = (
                        command[1] if Path(command[0]).name == "env" else command[0]
                    )
                    if Path(command[0]).name == "env":
                        python = shutil.which(python, path=DEFAULT_ENV_PATH) or python
                    return ["--python", python]
            except Exception:
                pass
        return ["--system"]

    def _pip(
        self,
        pip_cmd: list[str],
        quiet: bool = False,
        timeout: int | None = None,
        postinstall_scripts: bool = False,
        min_release_age: float = 7.0,
    ) -> subprocess.CompletedProcess:
        pip_abspath = self._require_installer_bin()

        uv_abspath = bin_abspath("uv", PATH=DEFAULT_ENV_PATH) or shutil.which("uv")
        pip_binary = os.getenv("PIP_BINARY")
        if pip_binary and Path(pip_binary).expanduser().is_absolute():
            uv_abspath = None
        subcommand, *pip_args = pip_cmd
        is_install = subcommand == "install"
        has_release_age_flag = self._install_args_have_option(
            pip_args,
            "--exclude-newer",
            "--uploaded-prior-to",
        )
        has_no_build_flag = self._install_args_have_option(pip_args, "--no-build")
        has_only_binary_flag = self._install_args_have_option(
            pip_args,
            "--only-binary",
        )

        # supply-chain security: compute ISO-8601 cutoff once, used by both uv and pip
        if is_install and min_release_age > 0:
            from datetime import datetime, timedelta, timezone

            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=min_release_age)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            cutoff = ""

        uv_cmd = [
            "pip",
            subcommand,
            *(
                ["--quiet"]
                if subcommand != "show" and (quiet or "--quiet" in pip_args)
                else []
            ),
            *(
                self._uv_pip_target_args()
                if subcommand in ("install", "show", "uninstall")
                else []
            ),
            # supply-chain security: --no-build prevents arbitrary code execution,
            # --exclude-newer rejects packages published too recently
            *(
                ["--no-build"]
                if is_install and not postinstall_scripts and not has_no_build_flag
                else []
            ),
            *(
                [f"--exclude-newer={cutoff}"]
                if is_install and cutoff and not has_release_age_flag
                else []
            ),
            *(
                arg
                for arg in pip_args
                if arg
                not in ("--no-input", "--disable-pip-version-check", "--quiet", "--yes")
            ),
        ]
        # supply-chain security for plain pip (no uv): --only-binary :all:
        # prevents sdist builds, --uploaded-prior-to enforces min release age
        # (pip >= 26.0 only, see pypa/pip#13625)
        if is_install and not uv_abspath:
            installer = self.INSTALLER_BINARY
            pip_ver = installer.loaded_version if installer else None
            if pip_ver and pip_ver == SemVer((999, 999, 999)):
                pip_ver = None
            pip_cmd = [
                subcommand,
                *(
                    ["--only-binary", ":all:"]
                    if not postinstall_scripts and not has_only_binary_flag
                    else []
                ),
                *(
                    [f"--uploaded-prior-to={cutoff}"]
                    if cutoff
                    and pip_ver is not None
                    and pip_ver >= SemVer((26, 0, 0))  # pyright: ignore[reportOperatorIssue]
                    and not has_release_age_flag
                    else []
                ),
                *pip_args,
            ]
        return self.exec(
            bin_name=uv_abspath or pip_abspath,
            cmd=uv_cmd if uv_abspath else pip_cmd,
            quiet=quiet,
            timeout=timeout,
        )

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        timeout: int | None = None,
    ) -> str:
        if self.pip_venv:
            self.setup(
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
                min_version=min_version,
            )

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
        if self._install_args_have_option(install_args, "--no-build", "--only-binary"):
            postinstall_scripts = False
        if self._install_args_have_option(
            install_args,
            "--exclude-newer",
            "--uploaded-prior-to",
        ):
            min_release_age = 0

        proc = self._pip(
            [
                "install",
                "--no-input",
                self.cache_arg,
                *self.pip_install_args,
                *install_args,
            ],
            timeout=timeout,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
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
        timeout: int | None = None,
    ) -> str:
        if self.pip_venv:
            self.setup(
                postinstall_scripts=postinstall_scripts,
                min_release_age=min_release_age,
                min_version=min_version,
            )

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
        if self._install_args_have_option(install_args, "--no-build", "--only-binary"):
            postinstall_scripts = False
        if self._install_args_have_option(
            install_args,
            "--exclude-newer",
            "--uploaded-prior-to",
        ):
            min_release_age = 0

        proc = self._pip(
            [
                "install",
                "--no-input",
                self.cache_arg,
                *self.pip_install_args,
                "--upgrade",
                *install_args,
            ],
            timeout=timeout,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
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
        install_args = install_args or self.get_install_args(bin_name)

        proc = self._pip(
            [
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

        # fallback to using pip show to get the site-packages bin path
        output_lines = (
            self._pip(
                ["show", "--no-input", str(bin_name)],
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

        # fallback to using pip show to get the version (slower)
        output_lines = (
            self._pip(
                ["show", "--no-input", str(bin_name)],
                quiet=False,
                timeout=timeout,
            )
            .stdout.strip()
            .split("\n")
        )
        # Name: yt-dlp
        # Version: 1.3.0
        # Location: /Volumes/NVME/Users/squash/Library/Python/3.11/lib/python/site-packages
        try:
            version_str = [
                line for line in output_lines if line.startswith("Version: ")
            ][0].split("Version: ", 1)[-1]
            return version_str
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
