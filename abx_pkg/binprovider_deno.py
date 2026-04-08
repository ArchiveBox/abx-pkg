#!/usr/bin/env python3

__package__ = "abx_pkg"

import os
import sys
import tempfile
from pathlib import Path
from typing import ClassVar, Self

from platformdirs import user_cache_path
from pydantic import Field, computed_field, model_validator

from .base_types import BinName, BinProviderName, InstallArgs, PATHStr
from .binprovider import BinProvider, env_flag_is_true, remap_kwargs
from .logging import format_subprocess_output
from .semver import SemVer


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "deno-cache"
try:
    _user_cache = user_cache_path("deno", "abx-pkg", ensure_exists=True)
    if os.access(_user_cache, os.W_OK):
        USER_CACHE_PATH = _user_cache
except Exception:
    pass


class DenoProvider(BinProvider):
    """Deno runtime + package manager provider.

    ``deno_root`` mirrors ``DENO_INSTALL_ROOT``: when set, ``deno install -g``
    lays out binaries under ``<deno_root>/bin``. ``deno_dir`` mirrors
    ``DENO_DIR`` for cache isolation.

    Security:
    - npm lifecycle scripts are *opt-in* in Deno (the opposite of npm).
      ``postinstall_scripts=True`` adds ``--allow-scripts``; the default
      is to skip them.
    - ``--minimum-dependency-age=<minutes>`` for ``min_release_age`` (Deno 2.5+).
    """

    name: BinProviderName = "deno"
    INSTALLER_BIN: BinName = "deno"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "deno_root"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABX_PKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    deno_root: Path | None = None  # mirrors $DENO_INSTALL_ROOT, defaults to ~/.deno
    deno_dir: Path | None = None  # mirrors $DENO_DIR for cache isolation

    cache_dir: Path = USER_CACHE_PATH

    deno_install_args: list[str] = ["--allow-all"]

    deno_default_scheme: str = "npm"  # 'npm' or 'jsr'

    def supports_min_release_age(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        threshold = SemVer.parse("2.5.0")
        installer = self.INSTALLER_BINARY
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.deno_root:
            bin_dir = self.deno_root / "bin"
            if not (bin_dir.is_dir() and os.access(bin_dir, os.R_OK)):
                return False
        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.deno_root

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        return self.deno_root / "bin" if self.deno_root else None

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.deno_root,),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_deno_root(self) -> Self:
        if self.deno_root:
            self.PATH = self._merge_PATH(self.deno_root / "bin")
        else:
            default_root = (
                Path(
                    os.environ.get("DENO_INSTALL_ROOT")
                    or (Path("~").expanduser() / ".deno"),
                )
                / "bin"
            )
            self.PATH = self._merge_PATH(default_root, PATH=self.PATH)
        return self

    def exec(self, bin_name, cmd=(), cwd: Path | str = ".", quiet=False, **kwargs):
        # Inject DENO_INSTALL_ROOT / DENO_DIR / DENO_TLS_CA_STORE.
        env = (kwargs.pop("env", None) or os.environ.copy()).copy()
        # Use the system trust store so jsr/npm registry TLS works on hosts
        # that ship corporate / sandboxed CA bundles.
        env.setdefault("DENO_TLS_CA_STORE", "system")
        if self.deno_root:
            self.deno_root.mkdir(parents=True, exist_ok=True)
            (self.deno_root / "bin").mkdir(parents=True, exist_ok=True)
            env["DENO_INSTALL_ROOT"] = str(self.deno_root)
        if self.deno_dir:
            self.deno_dir.mkdir(parents=True, exist_ok=True)
            env["DENO_DIR"] = str(self.deno_dir)
        return super().exec(
            bin_name=bin_name,
            cmd=cmd,
            cwd=cwd,
            quiet=quiet,
            env=env,
            **kwargs,
        )

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> None:
        self._ensure_writable_cache_dir(self.cache_dir)
        if self.deno_root:
            (self.deno_root / "bin").mkdir(parents=True, exist_ok=True)

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
        self.setup()
        installer_bin = self._require_installer_bin()
        postinstall_scripts = bool(postinstall_scripts)
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}@>={min_version}"
                if arg
                and not arg.startswith(("-", ".", "/"))
                and ":" not in arg.split("/")[0]
                and "@" not in arg.split("/")[-1]
                else arg
                for arg in install_args
            ]

        cmd: list[str] = ["install", *self.deno_install_args, "-g"]
        if not any(arg in ("-f", "--force") for arg in install_args):
            cmd.append("--force")
        if not any(arg in ("-n", "--name") for arg in install_args):
            cmd.extend(["-n", bin_name])
        if postinstall_scripts and not any(
            arg == "--allow-scripts" or arg.startswith("--allow-scripts=")
            for arg in install_args
        ):
            cmd.append("--allow-scripts")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not any(
                arg == "--minimum-dependency-age"
                or arg.startswith("--minimum-dependency-age=")
                for arg in install_args
            )
        ):
            cmd.append(
                f"--minimum-dependency-age={max(int(min_release_age * 24 * 60), 1)}",
            )
        # Auto-prefix bare names with the default scheme (npm: or jsr:).
        for arg in install_args:
            if (
                arg
                and not arg.startswith(("-", ".", "/"))
                and ":" not in arg.split("/")[0]
            ):
                cmd.append(f"{self.deno_default_scheme}:{arg}")
            else:
                cmd.append(arg)

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
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
        # ``deno install -gf`` re-installs from scratch, which is the
        # idiomatic update path for global executables.
        return self.default_install_handler(
            bin_name=bin_name,
            install_args=install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
            timeout=timeout,
        )

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
        proc = self.exec(
            bin_name=self._require_installer_bin(),
            cmd=["uninstall", "-g", bin_name],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", [bin_name], proc)
        return True


if __name__ == "__main__":
    # Usage:
    # ./binprovider_deno.py load cowsay
    # ./binprovider_deno.py install cowsay
    result = deno = DenoProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(deno, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)
