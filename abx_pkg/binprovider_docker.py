#!/usr/bin/env python3
__package__ = "abx_pkg"

import os
import json

from pathlib import Path
from typing import Any

from pydantic import model_validator, TypeAdapter, computed_field
from typing import Self

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs, HostBinPath
from .semver import SemVer
from .binprovider import BinProvider, remap_kwargs
from .logging import get_logger, log_subprocess_error

logger = get_logger(__name__)


DEFAULT_DOCKER_ROOT = Path(
    os.environ.get("ABX_PKG_DOCKER_ROOT", "~/.cache/abx-pkg/docker"),
).expanduser()


class DockerProvider(BinProvider):
    name: BinProviderName = "docker"
    INSTALLER_BIN: BinName = "docker"

    PATH: PATHStr = ""

    docker_shim_dir: Path = DEFAULT_DOCKER_ROOT / "bin"
    docker_run_args: list[str] = ["--rm", "-i"]

    @computed_field
    @property
    def is_valid(self) -> bool:
        return bool(self.INSTALLER_BIN_ABSPATH)

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.docker_shim_dir,),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_docker_shims(self) -> Self:
        docker_bin_dir = str(self.bin_dir())
        if docker_bin_dir not in self.PATH:
            self.PATH = TypeAdapter(PATHStr).validate_python(
                ":".join([*self.PATH.split(":"), docker_bin_dir]),
            )
        return self

    def bin_dir(self) -> Path:
        return self.docker_shim_dir

    def metadata_dir(self) -> Path:
        return self.bin_dir().parent / "metadata"

    def metadata_path(self, bin_name: str) -> Path:
        return self.metadata_dir() / f"{bin_name}.json"

    def setup(self) -> None:
        self.bin_dir().mkdir(parents=True, exist_ok=True)
        self.metadata_dir().mkdir(parents=True, exist_ok=True)

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
        self.metadata_path(bin_name).write_text(
            json.dumps(
                {
                    "image": image_ref,
                    "tag": self._image_tag(image_ref),
                },
            ),
            encoding="utf-8",
        )

    def _read_metadata(self, bin_name: str) -> dict[str, Any] | None:
        metadata_path = self.metadata_path(bin_name)
        if not metadata_path.is_file():
            return None
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def _write_shim(self, bin_name: str, image_ref: str) -> Path:
        wrapper_path = self.bin_dir() / bin_name
        docker_bin = self.INSTALLER_BIN_ABSPATH
        assert docker_bin, (
            f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host"
        )

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

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> str:
        self.setup()

        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        logs: list[str] = []
        for image_ref in install_args:
            proc = self.exec(
                bin_name=self.INSTALLER_BIN_ABSPATH,
                cmd=["pull", image_ref],
            )
            if proc.returncode != 0:
                log_subprocess_error(
                    logger,
                    f"{self.__class__.__name__} install",
                    proc.stdout,
                    proc.stderr,
                )
                raise Exception(
                    f"{self.__class__.__name__}: install got returncode {proc.returncode} while pulling {image_ref}",
                )
            logs.append((proc.stderr.strip() + "\n" + proc.stdout.strip()).strip())

        main_image = self._main_image_ref(bin_name, install_args)
        self._write_metadata(bin_name, main_image)
        self._write_shim(bin_name, main_image)

        return "\n".join(logs).strip()

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> str:
        return self.default_install_handler(
            bin_name=bin_name,
            install_args=install_args,
            **context,
        )

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
                f"{self.__class__.__name__} uninstall method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        wrapper_path = self.bin_dir() / bin_name
        wrapper_path.unlink(missing_ok=True)
        self.metadata_path(bin_name).unlink(missing_ok=True)

        main_image = self._main_image_ref(bin_name, install_args)
        for image_ref in install_args:
            proc = self.exec(
                bin_name=self.INSTALLER_BIN_ABSPATH,
                cmd=["image", "rm", "--force", image_ref],
                quiet=True,
            )
            if proc.returncode != 0 and image_ref == main_image:
                log_subprocess_error(
                    logger,
                    f"{self.__class__.__name__} uninstall",
                    proc.stdout,
                    proc.stderr,
                )
                raise Exception(
                    f"{self.__class__.__name__}: uninstall got returncode {proc.returncode} while removing {image_ref}",
                )

        return True

    def default_abspath_handler(
        self,
        bin_name: BinName,
        **context,
    ) -> HostBinPath | None:
        wrapper_path = self.bin_dir() / str(bin_name)
        if wrapper_path.is_file() and os.access(wrapper_path, os.R_OK):
            return TypeAdapter(HostBinPath).validate_python(wrapper_path)
        return super().default_abspath_handler(bin_name, **context)

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        **context,
    ) -> SemVer | None:
        metadata = self._read_metadata(str(bin_name))
        if metadata:
            version = SemVer.parse(metadata["tag"])
            if version:
                return version
        return None
