#!/usr/bin/env python3

__package__ = "abx_pkg"

import json
import os
import shutil
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import Field, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
    PATHStr,
    abx_pkg_install_root_default,
)
from .binprovider import (
    BinProvider,
    BinProviderOverrides,
    env_flag_is_true,
    remap_kwargs,
)
from .logging import format_subprocess_output, get_logger

logger = get_logger(__name__)


# Ultimate fallback when neither the constructor arg nor
# ``ABX_PKG_CHROMEWEBSTORE_ROOT`` nor ``ABX_PKG_LIB_DIR`` is set.
DEFAULT_CHROMEWEBSTORE_ROOT = Path("~/.cache/abx-pkg/chromewebstore").expanduser()
CHROME_UTILS_PATH = Path(__file__).with_name("js") / "chrome" / "chrome_utils.js"


class ChromeWebstoreProvider(BinProvider):
    name: BinProviderName = "chromewebstore"
    INSTALLER_BIN: BinName = "node"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "extensions_root"
    BIN_DIR_FIELD: ClassVar[str | None] = "extensions_dir"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(default=None, repr=False)

    # Default: ABX_PKG_CHROMEWEBSTORE_ROOT > ABX_PKG_LIB_DIR/chromewebstore > None.
    extensions_root: Path | None = abx_pkg_install_root_default("chromewebstore")
    extensions_dir: Path | None = None
    overrides: BinProviderOverrides = {
        "*": {
            "abspath": "self.chromewebstore_abspath_handler",
            "version": "self.chromewebstore_version_handler",
            "install_args": "self.chromewebstore_install_args_handler",
            "install": "self.chromewebstore_install_handler",
            "update": "self.chromewebstore_install_handler",
            "uninstall": "self.chromewebstore_uninstall_handler",
        },
    }

    @computed_field
    @property
    def install_root(self) -> Path:
        if self.extensions_root:
            return self.extensions_root
        if self.extensions_dir:
            return self.extensions_dir.parent
        return DEFAULT_CHROMEWEBSTORE_ROOT

    @computed_field
    @property
    def bin_dir(self) -> Path:
        return self.extensions_dir or (self.install_root / "extensions")

    @computed_field
    @property
    def is_valid(self) -> bool:
        return bool(self.INSTALLER_BIN_ABSPATH and CHROME_UTILS_PATH.exists())

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.bin_dir, self.install_root),
                preserve_root=True,
            )
        return self

    def supports_postinstall_disable(self, action) -> bool:
        return True

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version=None,
    ) -> None:
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.bin_dir.mkdir(parents=True, exist_ok=True)

    def chromewebstore_install_args_handler(
        self,
        bin_name: str,
        **context,
    ) -> list[str]:
        return [bin_name, f"--name={bin_name}"]

    def _cache_path(self, bin_name: str) -> Path:
        return self.bin_dir / f"{bin_name}.extension.json"

    def _cached_extension(self, bin_name: str) -> dict[str, Any]:
        cache_path = self._cache_path(bin_name)
        if not cache_path.exists():
            return {}
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return cached if isinstance(cached, dict) else {}

    def _extension_name(self, bin_name: str, install_args: list[str]) -> str:
        if len(install_args) > 1:
            raw_name = str(install_args[1])
            if raw_name.startswith("--name="):
                return raw_name.split("=", 1)[1] or bin_name
            return raw_name
        return bin_name

    def _extension_spec(self, bin_name: str) -> tuple[str, str, Path, Path, Path]:
        cached = self._cached_extension(bin_name)
        install_args = list(self.get_install_args(bin_name, quiet=True))
        webstore_id = str(
            cached["webstore_id"]
            if "webstore_id" in cached
            else (install_args[0] if install_args else bin_name),
        )
        extension_name = str(
            cached["name"]
            if "name" in cached
            else self._extension_name(bin_name, install_args),
        )
        unpacked_path = Path(
            cached["unpacked_path"]
            if "unpacked_path" in cached
            else (self.bin_dir / f"{webstore_id}__{extension_name}"),
        )
        crx_path = Path(
            cached["crx_path"]
            if "crx_path" in cached
            else (self.bin_dir / f"{webstore_id}__{extension_name}.crx"),
        )
        manifest_path = unpacked_path / "manifest.json"
        return webstore_id, extension_name, unpacked_path, crx_path, manifest_path

    def chromewebstore_abspath_handler(self, bin_name: str, **context) -> str | None:
        _, _, _, _, manifest_path = self._extension_spec(bin_name)
        if manifest_path.exists():
            return str(manifest_path)
        return None

    def chromewebstore_version_handler(
        self,
        bin_name: str,
        abspath: str | Path | None = None,
        **context,
    ) -> str | None:
        manifest_path = (
            Path(abspath) if abspath else self.get_abspath(bin_name, quiet=True)
        )
        if not manifest_path or not Path(manifest_path).exists():
            return None
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        return str(manifest.get("version") or "")

    @remap_kwargs({"packages": "install_args"})
    def chromewebstore_install_handler(
        self,
        bin_name: str,
        install_args: list[str] | tuple[str, ...] | None = None,
        timeout: int | None = None,
        **context,
    ) -> str:
        install_args = list(install_args or self.get_install_args(bin_name))
        if self.dry_run:
            return f"DRY_RUN would install Chrome Web Store extension {bin_name}"

        webstore_id = str(install_args[0] if install_args else bin_name)
        extension_name = self._extension_name(bin_name, install_args)
        installer_bin = self._require_installer_bin()

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                str(CHROME_UTILS_PATH),
                "installExtensionWithCache",
                webstore_id,
                extension_name,
            ],
            cwd=self.install_root,
            timeout=timeout if timeout is not None else self.install_timeout,
            env={
                **os.environ,
                "CHROME_EXTENSIONS_DIR": str(self.bin_dir),
                # Make node 22+ honor HTTP(S)_PROXY env vars when fetching
                # extensions; ``undici``'s ``fetch`` does not consult them
                # without this flag, which silently breaks downloads on any
                # host that runs behind an outbound HTTP proxy.
                "NODE_USE_ENV_PROXY": "1",
            },
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", bin_name, proc)

        cache_path = self._cache_path(bin_name)
        if not cache_path.exists():
            raise FileNotFoundError(
                f"{self.__class__.__name__} did not produce cache metadata at {cache_path}",
            )

        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def chromewebstore_uninstall_handler(
        self,
        bin_name: str,
        install_args: list[str] | tuple[str, ...] | None = None,
        **context,
    ) -> bool:
        cache_path = self._cache_path(bin_name)
        _, _, unpacked_path, crx_path, _ = self._extension_spec(bin_name)

        if cache_path.exists():
            cache_path.unlink(missing_ok=True)
        if crx_path.exists():
            crx_path.unlink(missing_ok=True)
        if unpacked_path.exists():
            shutil.rmtree(unpacked_path, ignore_errors=True)
        return True
