import tempfile
from pathlib import Path

import pytest

from abx_pkg import Binary, DockerProvider, SemVer
from abx_pkg.exceptions import BinaryInstallError


@pytest.mark.docker_required
class TestDockerProvider:
    def test_bin_dir_alias_without_explicit_install_root_uses_parent_as_root(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir) / "custom-bin"
            provider = DockerProvider.model_validate(
                {
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )

            installed = provider.install("shellcheck")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == bin_dir.parent
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert provider.metadata_dir() == bin_dir.parent / "metadata"
            assert provider.metadata_path("shellcheck").is_file()

    def test_install_root_alias_without_explicit_bin_dir_uses_root_bin(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "docker-root"
            provider = DockerProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )

            installed = provider.install("shellcheck")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert provider.metadata_dir() == install_root / "metadata"
            assert provider.metadata_path("shellcheck").is_file()

    def test_install_root_and_bin_dir_aliases_install_the_shim_in_the_requested_location(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "docker-root"
            bin_dir = Path(temp_dir) / "custom-bin"
            provider = DockerProvider.model_validate(
                {
                    "install_root": install_root,
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )

            installed = provider.install("shellcheck")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert provider.metadata_dir() == install_root / "metadata"
            assert provider.metadata_path("shellcheck").is_file()

    def test_explicit_docker_shim_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = DockerProvider(
                docker_shim_dir=temp_dir_path / "ambient-docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            ambient_installed = ambient_provider.install("shellcheck")
            assert ambient_installed is not None

            docker_shim_dir = temp_dir_path / "docker/bin"
            provider = DockerProvider(
                PATH=str(ambient_provider.bin_dir),
                docker_shim_dir=docker_shim_dir,
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )

            installed = provider.install("shellcheck")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_abspath is not None
            assert provider.install_root == docker_shim_dir.parent
            assert provider.bin_dir == docker_shim_dir
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir
            assert installed.loaded_version == SemVer("0.10.0")

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(
                docker_shim_dir=Path(temp_dir) / "docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            test_machine.exercise_provider_lifecycle(provider, bin_name="shellcheck")

    def test_provider_direct_min_version_revalidates_final_installed_image(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(
                docker_shim_dir=Path(temp_dir) / "docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            with pytest.raises(ValueError):
                provider.install("shellcheck", min_version=SemVer("999.0.0"))

            loaded = provider.load("shellcheck", quiet=True, nocache=True)
            test_machine.assert_shallow_binary_loaded(loaded)
            assert loaded is not None
            assert loaded.loaded_version == SemVer("0.10.0")

    def test_latest_tag_falls_back_to_runtime_version_probe(self, test_machine):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(
                docker_shim_dir=Path(temp_dir) / "docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:latest"]},
                },
            )

            installed = provider.install("shellcheck")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert installed.loaded_version is not None

    def test_unsupported_security_controls_fail_closed_and_binary_override_wins(
        self,
        test_machine,
    ):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            with pytest.raises(RuntimeError):
                DockerProvider(
                    docker_shim_dir=Path(temp_dir) / "bad/bin",
                    postinstall_scripts=False,
                    min_release_age=0,
                ).get_provider_with_overrides(
                    overrides={
                        "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                    },
                ).install("shellcheck")

            with pytest.raises(RuntimeError):
                DockerProvider(
                    docker_shim_dir=Path(temp_dir) / "bad-age/bin",
                    postinstall_scripts=True,
                    min_release_age=1,
                ).get_provider_with_overrides(
                    overrides={
                        "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                    },
                ).install("shellcheck")

            binary = Binary(
                name="shellcheck",
                binproviders=[
                    DockerProvider(
                        docker_shim_dir=Path(temp_dir) / "ok/bin",
                        postinstall_scripts=False,
                        min_release_age=1,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={
                    "docker": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

            failing_binary = Binary(
                name="shellcheck",
                binproviders=[
                    DockerProvider(
                        docker_shim_dir=Path(temp_dir) / "failing/bin",
                        postinstall_scripts=False,
                        min_release_age=1,
                    ),
                ],
                postinstall_scripts=False,
                min_release_age=1,
                overrides={
                    "docker": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            with pytest.raises(BinaryInstallError):
                failing_binary.install()

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="shellcheck",
                binproviders=[
                    DockerProvider(
                        docker_shim_dir=Path(temp_dir) / "docker/bin",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
                overrides={
                    "docker": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_shellcheck(self, test_machine):
        test_machine.require_docker_daemon()

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(
                docker_shim_dir=Path(temp_dir) / "docker/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "shellcheck": {"install_args": ["koalaman/shellcheck:v0.10.0"]},
                },
            )
            test_machine.exercise_provider_dry_run(provider, bin_name="shellcheck")
