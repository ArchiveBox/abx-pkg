#!/usr/bin/env python3
__package__ = "abx_pkg"

import os
import json

from pathlib import Path
from typing import Any

from pydantic import Field, model_validator, TypeAdapter
from typing import Self

from .base_types import (
    DEFAULT_LIB_DIR,
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    HostBinPath,
    abx_pkg_install_root_default,
)
from .semver import SemVer
from .binprovider import BinProvider, remap_kwargs
from .logging import format_subprocess_output


# Ultimate fallback when neither the constructor arg nor
# ``ABX_PKG_DOCKER_ROOT`` nor ``ABX_PKG_LIB_DIR`` is set.
DEFAULT_DOCKER_ROOT = DEFAULT_LIB_DIR / "docker"


class DockerProvider(BinProvider):
    name: BinProviderName = "docker"
    INSTALLER_BIN: BinName = "docker"

    PATH: PATHStr = ""

    # Default: ABX_PKG_DOCKER_ROOT > ABX_PKG_LIB_DIR/docker > None.
    install_root: Path | None = Field(
        default_factory=lambda: abx_pkg_install_root_default("docker"),
        validation_alias="docker_root",
    )
    bin_dir: Path | None = Field(default=None, validation_alias="docker_shim_dir")
    docker_run_args: list[str] = ["--rm", "-i"]

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.install_root is None:
            self.install_root = DEFAULT_DOCKER_ROOT
        if self.bin_dir is None:
            self.bin_dir = self.install_root / "bin"
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.bin_dir,),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_docker_shims(self) -> Self:
        bin_dir = self.bin_dir
        assert bin_dir is not None
        self.PATH = self._merge_PATH(
            bin_dir,
            PATH=self.PATH,
            prepend=True,
        )
        return self

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        bin_dir = self.bin_dir
        install_root = self.install_root
        assert bin_dir is not None
        assert install_root is not None
        bin_dir.mkdir(parents=True, exist_ok=True)
        (install_root / "metadata").mkdir(parents=True, exist_ok=True)

    def default_install_args_handler(self, bin_name: BinName, **context) -> InstallArgs:
        return [f"{bin_name}:latest"]

    @remap_kwargs({"packages": "install_args"})
    def _main_image_ref(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
    ) -> str:
        package_list = list(install_args or self.get_install_args(bin_name))
        assert package_list, (
            f"{self.__class__.__name__} requires at least one docker image ref for {bin_name}"
        )
        return str(package_list[0])

    def _image_tag(self, image_ref: str) -> str:
        image_without_digest = image_ref.split("@", 1)[0]
        last_component = image_without_digest.rsplit("/", 1)[-1]
        if ":" in last_component:
            return image_without_digest.rsplit(":", 1)[-1]
        return "latest"

    def _write_metadata(self, bin_name: str, image_ref: str) -> None:
        install_root = self.install_root
        assert install_root is not None
        (install_root / "metadata" / f"{bin_name}.json").write_text(
            json.dumps(
                {
                    "image": image_ref,
                    "tag": self._image_tag(image_ref),
                },
            ),
            encoding="utf-8",
        )

    def _read_metadata(self, bin_name: str) -> dict[str, Any] | None:
        install_root = self.install_root
        assert install_root is not None
        metadata_path = install_root / "metadata" / f"{bin_name}.json"
        if not metadata_path.is_file():
            return None
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def _write_shim(self, bin_name: str, image_ref: str) -> Path:
        bin_dir = self.bin_dir
        assert bin_dir is not None
        wrapper_path = bin_dir / bin_name
        docker_bin = self.INSTALLER_BINARY().loaded_abspath
        assert docker_bin

        wrapper_path.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env sh",
                    "set -eu",
                    'workdir="${PWD:-$(pwd)}"',
                    f'exec "{docker_bin}" run {" ".join(self.docker_run_args)} --user "$(id -u):$(id -g)" -v "$workdir:$workdir" -w "$workdir" "{image_ref}" "$@"',
                    "",
                ],
            ),
            encoding="utf-8",
        )
        wrapper_path.chmod(0o755)
        return wrapper_path

    @staticmethod
    def _should_repair_failed_pull(output: str) -> bool:
        return any(
            marker in output
            for marker in (
                "unable to prepare extraction snapshot",
                "failed to prepare extraction snapshot",
                "target snapshot",
                "missing parent",
            )
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
        self.setup(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
        )

        install_args = install_args or self.get_install_args(bin_name)
        installer_bin = self.INSTALLER_BINARY().loaded_abspath
        assert installer_bin

        logs: list[str] = []
        for image_ref in install_args:
            proc = self.exec(
                bin_name=installer_bin,
                cmd=["pull", image_ref],
                timeout=timeout,
            )
            if proc.returncode != 0:
                pull_output = format_subprocess_output(proc.stdout, proc.stderr)
                if self._should_repair_failed_pull(pull_output):
                    repair_proc = self.exec(
                        bin_name=installer_bin,
                        cmd=["image", "rm", "--force", image_ref],
                        quiet=True,
                        timeout=timeout,
                    )
                    logs.extend(
                        output
                        for output in (
                            pull_output,
                            format_subprocess_output(
                                repair_proc.stdout,
                                repair_proc.stderr,
                            ),
                        )
                        if output
                    )
                    proc = self.exec(
                        bin_name=installer_bin,
                        cmd=["pull", image_ref],
                        timeout=timeout,
                    )
                if proc.returncode != 0:
                    self._raise_proc_error("install", image_ref, proc)
            logs.append(format_subprocess_output(proc.stdout, proc.stderr))

        main_image = self._main_image_ref(bin_name, install_args)
        self._write_metadata(bin_name, main_image)
        self._write_shim(bin_name, main_image)

        return "\n".join(logs).strip()

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
        install_args = install_args or self.get_install_args(bin_name)
        installer_bin = self.INSTALLER_BINARY().loaded_abspath
        assert installer_bin

        bin_dir = self.bin_dir
        install_root = self.install_root
        assert bin_dir is not None
        assert install_root is not None
        wrapper_path = bin_dir / bin_name
        wrapper_path.unlink(missing_ok=True)
        (install_root / "metadata" / f"{bin_name}.json").unlink(missing_ok=True)

        main_image = self._main_image_ref(bin_name, install_args)
        for image_ref in install_args:
            proc = self.exec(
                bin_name=installer_bin,
                cmd=["image", "rm", "--force", image_ref],
                quiet=True,
                timeout=timeout,
            )
            if proc.returncode != 0 and image_ref == main_image:
                self._raise_proc_error("uninstall", image_ref, proc)

        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        bin_dir = self.bin_dir
        assert bin_dir is not None
        wrapper_path = bin_dir / str(bin_name)
        if wrapper_path.is_file() and os.access(wrapper_path, os.R_OK):
            return TypeAdapter(HostBinPath).validate_python(wrapper_path)
        abspath = super().default_abspath_handler(bin_name, **context)
        if abspath is None:
            return None
        return TypeAdapter(HostBinPath).validate_python(abspath)

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> SemVer | None:
        metadata = self._read_metadata(str(bin_name))
        if metadata:
            parsed_tag = SemVer.parse(str(metadata["tag"]))
            if parsed_tag:
                return parsed_tag

        abspath = abspath or self.get_abspath(bin_name, quiet=True)
        if not abspath:
            return None

        try:
            version = super().default_version_handler(
                bin_name,
                abspath=abspath,
                timeout=timeout,
                **context,
            )
            return SemVer.parse(version) if version is not None else None
        except ValueError:
            return None
