#!/usr/bin/env python3

__package__ = "abx_pkg"

import os
import shlex
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import Field, TypeAdapter, computed_field, model_validator

from .base_types import BinName, BinProviderName, HostBinPath, InstallArgs, PATHStr
from .binprovider import BinProviderOverrides, EnvProvider, HandlerType, remap_kwargs
from .logging import format_subprocess_output


DEFAULT_CUSTOM_ROOT = Path(
    os.environ.get("ABX_PKG_CUSTOM_ROOT", "~/.cache/abx-pkg/custom"),
).expanduser()


class CustomProvider(EnvProvider):
    name: BinProviderName = "custom"
    INSTALLER_BIN: BinName = "sh"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "custom_root"
    BIN_DIR_FIELD: ClassVar[str | None] = "custom_bin_dir"

    PATH: PATHStr = ""
    min_release_age: float = Field(default=0, repr=False)

    custom_root: Path | None = None
    custom_bin_dir: Path | None = None

    overrides: BinProviderOverrides = {
        "*": {
            "version": "self.custom_version_handler",
            "abspath": "self.default_abspath_handler",
            "install_args": "self.default_install_args_handler",
            "install": "self.default_install_handler",
            "update": "self.default_update_handler",
            "uninstall": "self.default_uninstall_handler",
        },
    }

    @computed_field
    @property
    def install_root(self) -> Path:
        if self.custom_root:
            return self.custom_root
        if self.custom_bin_dir:
            return self.custom_bin_dir.parent
        return DEFAULT_CUSTOM_ROOT

    @computed_field
    @property
    def bin_dir(self) -> Path:
        return self.custom_bin_dir or (self.install_root / "bin")

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.bin_dir, self.install_root),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_custom_bin_dir(self) -> Self:
        self.PATH = self._merge_PATH(self.bin_dir, PATH=self.PATH, prepend=True)
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

    def _literal_override_value(
        self,
        bin_name: str,
        handler_type: HandlerType,
    ) -> Any:
        for overrides_for_bin in (
            self.overrides.get(bin_name, {}),
            self.overrides.get("*", {}),
        ):
            value = overrides_for_bin.get(handler_type)
            if value is None:
                continue
            if callable(value):
                continue
            if isinstance(value, str) and (
                value.startswith("self.") or value.startswith("BinProvider.")
            ):
                continue
            return value
        return None

    def _get_shell_command(
        self,
        bin_name: str,
        handler_type: HandlerType,
    ) -> str | None:
        value = self._literal_override_value(bin_name, handler_type)
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return shlex.join(str(part) for part in value)
        return str(value)

    def _get_handler_for_action(
        self,
        bin_name: BinName,
        handler_type: HandlerType,
    ):
        if handler_type in ("install", "update", "uninstall"):
            literal = self._literal_override_value(str(bin_name), handler_type)
            if literal is not None:
                return getattr(self, f"default_{handler_type}_handler")
        return super()._get_handler_for_action(bin_name, handler_type)

    def _custom_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "INSTALL_ROOT": str(self.install_root),
            "BIN_DIR": str(self.bin_dir),
            "CUSTOM_INSTALL_ROOT": str(self.install_root),
            "CUSTOM_BIN_DIR": str(self.bin_dir),
        }

    def custom_version_handler(
        self,
        bin_name: str,
        abspath: str | Path | None = None,
        **context,
    ) -> str | None:
        try:
            validated_abspath = (
                TypeAdapter(HostBinPath).validate_python(abspath) if abspath else None
            )
            version = super().default_version_handler(
                bin_name,
                abspath=validated_abspath,
                **context,
            )
            if version:
                return str(version)
        except Exception:
            pass

        if abspath or self.get_abspath(bin_name, quiet=True):
            fallback = self._literal_override_value(bin_name, "version")
            if fallback is not None:
                return str(fallback)
            return "0.0.1"
        return None

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        **context,
    ) -> str:
        command = self._get_shell_command(str(bin_name), "install")
        if not command:
            raise ValueError(
                "CustomProvider requires a literal overrides.install shell command",
            )

        proc = self.exec(
            bin_name=self._require_installer_bin(),
            cmd=["-c", command],
            cwd=self.install_root,
            timeout=timeout if timeout is not None else self.install_timeout,
            env=self._custom_env(),
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", bin_name, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        **context,
    ) -> str:
        command = self._get_shell_command(
            str(bin_name),
            "update",
        ) or self._get_shell_command(
            str(bin_name),
            "install",
        )
        if not command:
            raise ValueError(
                "CustomProvider requires a literal overrides.install or overrides.update shell command",
            )

        proc = self.exec(
            bin_name=self._require_installer_bin(),
            cmd=["-c", command],
            cwd=self.install_root,
            timeout=timeout if timeout is not None else self.install_timeout,
            env=self._custom_env(),
        )
        if proc.returncode != 0:
            self._raise_proc_error("update", bin_name, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        **context,
    ) -> bool:
        command = self._get_shell_command(str(bin_name), "uninstall")
        if command:
            proc = self.exec(
                bin_name=self._require_installer_bin(),
                cmd=["-c", command],
                cwd=self.install_root,
                timeout=timeout if timeout is not None else self.install_timeout,
                env=self._custom_env(),
            )
            if proc.returncode != 0:
                self._raise_proc_error("uninstall", bin_name, proc)

        (self.bin_dir / str(bin_name)).unlink(missing_ok=True)
        return True
