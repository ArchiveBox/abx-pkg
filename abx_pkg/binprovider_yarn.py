#!/usr/bin/env python3

__package__ = "abx_pkg"

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import ClassVar, Self

from platformdirs import user_cache_path
from pydantic import Field, TypeAdapter, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abx_pkg_install_root_default,
    bin_abspath,
)
from .binprovider import BinProvider, env_flag_is_true, remap_kwargs
from .logging import format_subprocess_output
from .semver import SemVer


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "yarn-cache"
try:
    _user_cache = user_cache_path("yarn", "abx-pkg", ensure_exists=True)
    if os.access(_user_cache, os.W_OK):
        USER_CACHE_PATH = _user_cache
except Exception:
    pass


# No forced fallback — when no explicit prefix is set, yarn uses its
# native global mode (yarn global add / yarn global bin / etc.).


class YarnProvider(BinProvider):
    """Yarn package manager provider (Yarn 4 / Berry recommended).

    Yarn 4 is workspace-based: every install happens inside a project dir
    containing a ``package.json`` and ``.yarnrc.yml``. This provider auto-
    initializes a managed workspace under ``yarn_prefix`` on first use,
    configures ``nodeLinker: node-modules`` so binaries land in
    ``<yarn_prefix>/node_modules/.bin``, and writes the ``npmMinimalAgeGate``
    security setting from ``min_release_age``.

    Yarn classic (1.x) does not support ``npmMinimalAgeGate`` /
    ``--mode skip-build``; on those hosts ``supports_min_release_age`` /
    ``supports_postinstall_disable`` return ``False`` and the runtime falls
    back to a plain install while logging a warning.
    """

    name: BinProviderName = "yarn"
    INSTALLER_BIN: BinName = "yarn"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "yarn_prefix"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABX_PKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    # Workspace dir. Default: ABX_PKG_YARN_ROOT > ABX_PKG_LIB_DIR/yarn > None.
    yarn_prefix: Path | None = abx_pkg_install_root_default("yarn")

    cache_dir: Path = USER_CACHE_PATH

    yarn_install_args: list[str] = []

    def supports_min_release_age(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        # npmMinimalAgeGate landed in Yarn 4.10
        threshold = SemVer.parse("4.10.0")
        installer = self.INSTALLER_BINARY
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    def supports_postinstall_disable(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        # Yarn 2+ supports the enableScripts setting and --mode skip-build
        threshold = SemVer.parse("2.0.0")
        installer = self.INSTALLER_BINARY
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Resolve the yarn executable, honoring ``YARN_BINARY`` for explicit overrides."""
        if self._INSTALLER_BIN_ABSPATH:
            return self._INSTALLER_BIN_ABSPATH

        manual_binary = os.environ.get("YARN_BINARY")
        if manual_binary and os.path.isabs(manual_binary):
            try:
                valid_abspath = TypeAdapter(HostBinPath).validate_python(
                    Path(manual_binary).resolve(),
                )
                self._INSTALLER_BIN_ABSPATH = valid_abspath
                return valid_abspath
            except Exception:
                return None

        abspath = bin_abspath(self.INSTALLER_BIN, PATH=self.PATH) or bin_abspath(
            self.INSTALLER_BIN,
        )
        if not abspath:
            return None

        valid_abspath = TypeAdapter(HostBinPath).validate_python(abspath)
        if valid_abspath:
            self._INSTALLER_BIN_ABSPATH = valid_abspath
        return valid_abspath

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.yarn_prefix:
            bin_dir = self.yarn_prefix / "node_modules" / ".bin"
            if not (bin_dir.is_dir() and os.access(bin_dir, os.R_OK)):
                return False
        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.yarn_prefix

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        return self.yarn_prefix / "node_modules" / ".bin" if self.yarn_prefix else None

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.yarn_prefix,),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_yarn_prefix(self) -> Self:
        if self.yarn_prefix:
            self.PATH = self._merge_PATH(self.yarn_prefix / "node_modules" / ".bin")
        return self

    def exec(self, bin_name, cmd=(), cwd: Path | str = ".", quiet=False, **kwargs):
        # Yarn 4 expects to be invoked from inside a workspace dir, so default
        # cwd to <yarn_prefix>. Also pin global folders into our cache_dir so
        # parallel test workspaces share the same store.
        env = (kwargs.pop("env", None) or os.environ.copy()).copy()
        env.setdefault("YARN_ENABLE_TELEMETRY", "0")
        env.setdefault("YARN_ENABLE_GLOBAL_CACHE", "1")
        env.setdefault("YARN_GLOBAL_FOLDER", str(self.cache_dir))
        env.setdefault("YARN_CACHE_FOLDER", str(self.cache_dir / "v6"))
        if cwd == "." and self.yarn_prefix:
            self.yarn_prefix.mkdir(parents=True, exist_ok=True)
            cwd = self.yarn_prefix
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
        prefix = self.yarn_prefix
        if not prefix:
            raise TypeError(
                "YarnProvider.setup requires yarn_prefix to be set "
                "(pass install_root= or set ABX_PKG_YARN_ROOT / ABX_PKG_LIB_DIR)"
            )
        prefix.mkdir(parents=True, exist_ok=True)
        package_json = prefix / "package.json"
        if not package_json.exists():
            # Note: do NOT write a ``packageManager`` field — Yarn 1.22 reads
            # it as an opt-in to corepack and refuses to install if the
            # running yarn version doesn't match.
            package_json.write_text(
                json.dumps(
                    {
                        "name": "abx-pkg-yarn-workspace",
                        "version": "0.0.0",
                        "private": True,
                    },
                    indent=2,
                )
                + "\n",
            )
        # Yarn 2+ uses .yarnrc.yml; pin nodeLinker so binaries end up in
        # node_modules/.bin instead of the PnP store.
        installer = self.INSTALLER_BINARY
        version = installer.loaded_version if installer else None
        berry_threshold = SemVer.parse("2.0.0")
        if version and berry_threshold and version >= berry_threshold:
            yarnrc = prefix / ".yarnrc.yml"
            existing = yarnrc.read_text() if yarnrc.exists() else ""
            if "nodeLinker:" not in existing:
                yarnrc.write_text(
                    (existing.rstrip("\n") + "\n" if existing else "")
                    + "nodeLinker: node-modules\n",
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

        installer = self.INSTALLER_BINARY
        version = installer.loaded_version if installer else None
        berry_threshold = SemVer.parse("2.0.0")
        is_berry = (
            version is not None
            and berry_threshold is not None
            and version >= berry_threshold
        )

        # Rewrite ``.yarnrc.yml`` (Yarn 2+ only) so npmMinimalAgeGate /
        # enableScripts always reflect the latest provider/binary defaults.
        if is_berry and version is not None:
            prefix = self.yarn_prefix
            yarnrc = prefix / ".yarnrc.yml"
            existing = yarnrc.read_text() if yarnrc.exists() else ""
            kept = [
                line
                for line in existing.splitlines()
                if not line.strip().startswith(("npmMinimalAgeGate:", "enableScripts:"))
            ]
            age_threshold = SemVer.parse("4.10.0")
            if (
                min_release_age is not None
                and min_release_age > 0
                and age_threshold is not None
                and version >= age_threshold
            ):
                duration = (
                    f"{int(min_release_age)}d"
                    if min_release_age >= 1 and float(min_release_age).is_integer()
                    else f"{max(int(min_release_age * 24 * 60), 1)}m"
                )
                kept.append(f"npmMinimalAgeGate: {duration}")
            if not postinstall_scripts:
                kept.append("enableScripts: false")
            content = "\n".join(kept)
            yarnrc.write_text(content + "\n" if content else "")

        cmd = ["add", *self.yarn_install_args, *install_args]
        if is_berry and not postinstall_scripts:
            cmd = [
                "add",
                *self.yarn_install_args,
                "--mode",
                "skip-build",
                *install_args,
            ]

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

        installer = self.INSTALLER_BINARY
        version = installer.loaded_version if installer else None
        berry_threshold = SemVer.parse("2.0.0")
        is_berry = (
            version is not None
            and berry_threshold is not None
            and version >= berry_threshold
        )

        if is_berry and version is not None:
            prefix = self.yarn_prefix
            yarnrc = prefix / ".yarnrc.yml"
            existing = yarnrc.read_text() if yarnrc.exists() else ""
            kept = [
                line
                for line in existing.splitlines()
                if not line.strip().startswith(("npmMinimalAgeGate:", "enableScripts:"))
            ]
            age_threshold = SemVer.parse("4.10.0")
            if (
                min_release_age is not None
                and min_release_age > 0
                and age_threshold is not None
                and version >= age_threshold
            ):
                duration = (
                    f"{int(min_release_age)}d"
                    if min_release_age >= 1 and float(min_release_age).is_integer()
                    else f"{max(int(min_release_age * 24 * 60), 1)}m"
                )
                kept.append(f"npmMinimalAgeGate: {duration}")
            if not postinstall_scripts:
                kept.append("enableScripts: false")
            content = "\n".join(kept)
            yarnrc.write_text(content + "\n" if content else "")

            cmd = ["up", *self.yarn_install_args, *install_args]
            if not postinstall_scripts:
                cmd = [
                    "up",
                    *self.yarn_install_args,
                    "--mode",
                    "skip-build",
                    *install_args,
                ]
        else:
            cmd = ["upgrade", *self.yarn_install_args, *install_args]

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
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
        installer_bin = self._require_installer_bin()
        install_args = install_args or self.get_install_args(bin_name)

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["remove", *self.yarn_install_args, *install_args],
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
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass

        if not self.INSTALLER_BIN_ABSPATH:
            return None

        if self.yarn_prefix:
            candidate = self.yarn_prefix / "node_modules" / ".bin" / str(bin_name)
            if candidate.exists():
                return TypeAdapter(HostBinPath).validate_python(candidate)
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

        if not self.INSTALLER_BIN_ABSPATH:
            return None

        if not self.yarn_prefix:
            return None
        install_args = self.get_install_args(str(bin_name), **context) or [
            str(bin_name),
        ]
        main_package = install_args[0]
        package = (
            "@" + main_package[1:].split("@", 1)[0]
            if main_package.startswith("@")
            else main_package.split("@", 1)[0]
        )
        package_json = self.yarn_prefix / "node_modules" / package / "package.json"
        if package_json.exists():
            try:
                return json.loads(package_json.read_text())["version"]
            except Exception:
                return None
        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_yarn.py load zx
    # ./binprovider_yarn.py install zx
    result = yarn = YarnProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(yarn, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)
