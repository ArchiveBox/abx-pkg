import tempfile
from pathlib import Path

from abx_pkg import Binary, PuppeteerProvider


PUPPETEER_CHROMEDRIVER_ARGS = ["chromedriver@stable"]


class TestPuppeteerProvider:
    def test_install_root_alias_without_explicit_bin_dir_uses_root_bin(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "puppeteer-root"
            provider = PuppeteerProvider.model_validate(
                {
                    "install_root": install_root,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "chromedriver": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS},
                },
            )

            installed = provider.install("chromedriver")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == install_root / "bin"
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_install_root_and_bin_dir_aliases_install_into_the_requested_paths(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "puppeteer-root"
            bin_dir = Path(temp_dir) / "custom-bin"
            provider = PuppeteerProvider.model_validate(
                {
                    "install_root": install_root,
                    "bin_dir": bin_dir,
                    "postinstall_scripts": True,
                    "min_release_age": 0,
                },
            ).get_provider_with_overrides(
                overrides={
                    "chromedriver": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS},
                },
            )

            installed = provider.install("chromedriver")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert provider.install_root == install_root
            assert provider.bin_dir == bin_dir
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir

    def test_explicit_browser_bin_dir_takes_precedence_over_existing_PATH_entries(
        self,
        test_machine,
    ):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            ambient_provider = PuppeteerProvider(
                puppeteer_root=temp_dir_path / "ambient-root",
                browser_bin_dir=temp_dir_path / "ambient-root/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "chromedriver": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS},
                },
            )
            ambient_installed = ambient_provider.install("chromedriver")
            assert ambient_installed is not None

            provider = PuppeteerProvider(
                PATH=str(ambient_provider.bin_dir),
                puppeteer_root=temp_dir_path / "puppeteer-root",
                browser_bin_dir=temp_dir_path / "custom-bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "chromedriver": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS},
                },
            )

            installed = provider.install("chromedriver")

            test_machine.assert_shallow_binary_loaded(installed)
            assert installed is not None
            assert provider.bin_dir == temp_dir_path / "custom-bin"
            assert installed.loaded_abspath is not None
            assert installed.loaded_abspath.parent == provider.bin_dir
            assert ambient_installed.loaded_abspath is not None
            assert ambient_installed.loaded_abspath.parent == ambient_provider.bin_dir

    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PuppeteerProvider(
                puppeteer_root=Path(temp_dir) / "puppeteer-root",
                browser_bin_dir=Path(temp_dir) / "puppeteer-root/bin",
                postinstall_scripts=True,
                min_release_age=0,
            ).get_provider_with_overrides(
                overrides={
                    "chromedriver": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS},
                },
            )

            test_machine.exercise_provider_lifecycle(provider, bin_name="chromedriver")

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")

        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Binary(
                name="chromedriver",
                binproviders=[
                    PuppeteerProvider(
                        puppeteer_root=Path(temp_dir) / "puppeteer-root",
                        browser_bin_dir=Path(temp_dir) / "puppeteer-root/bin",
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                overrides={"puppeteer": {"install_args": PUPPETEER_CHROMEDRIVER_ARGS}},
                postinstall_scripts=True,
                min_release_age=0,
            )

            test_machine.exercise_binary_lifecycle(binary)
