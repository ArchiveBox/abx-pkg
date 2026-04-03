import pytest

from abx_pkg import Binary, BrewProvider, SemVer
from abx_pkg.exceptions import BinaryInstallError, BinaryLoadOrInstallError


def _pick_formula_for_live_cycle() -> str:
    probe = BrewProvider(postinstall_scripts=True, min_release_age=0)
    candidates = ("hello", "jq", "watch", "fzy")
    for formula in candidates:
        if probe.load(formula, quiet=True, nocache=True) is None:
            return formula
    for formula in candidates:
        try:
            probe.uninstall(formula, quiet=True, nocache=True)
        except Exception:
            continue
        if probe.load(formula, quiet=True, nocache=True) is None:
            return formula
    raise AssertionError(
        "Unable to find a brew formula candidate that can be installed on the test machine",
    )


class TestBrewProvider:
    def test_provider_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("brew")
        formula = _pick_formula_for_live_cycle()
        provider = BrewProvider(postinstall_scripts=True, min_release_age=0)

        installed, _ = test_machine.exercise_provider_lifecycle(
            provider,
            bin_name=formula,
        )
        assert provider.install_root == provider.brew_prefix
        assert provider.bin_dir == provider.brew_prefix / "bin"
        assert installed.loaded_abspath is not None
        assert installed.loaded_abspath.is_relative_to(provider.install_root)

    def test_unsupported_min_release_age_fails_closed_and_binary_override_wins(
        self,
        test_machine,
    ):
        test_machine.require_tool("brew")
        formula = _pick_formula_for_live_cycle()

        with pytest.raises(RuntimeError):
            BrewProvider().install(formula)

        provider_for_cleanup = BrewProvider(
            postinstall_scripts=False,
            min_release_age=0,
        )
        try:
            binary = Binary(
                name=formula,
                binproviders=[BrewProvider()],
                postinstall_scripts=False,
                min_release_age=0,
            )
            installed = binary.install()
            test_machine.assert_shallow_binary_loaded(installed)

            failing_binary = Binary(
                name=formula,
                binproviders=[BrewProvider()],
            )
            with pytest.raises(BinaryInstallError):
                failing_binary.install()
        finally:
            provider_for_cleanup.uninstall(formula, quiet=True, nocache=True)

    def test_postinstall_disable_is_live_and_min_version_is_enforced(
        self,
        test_machine,
    ):
        test_machine.require_tool("brew")
        formula = _pick_formula_for_live_cycle()
        provider = BrewProvider(postinstall_scripts=True, min_release_age=0)

        installed = provider.install(
            formula,
            postinstall_scripts=False,
            min_release_age=0,
            nocache=True,
        )
        test_machine.assert_shallow_binary_loaded(installed)

        with pytest.raises(ValueError):
            provider.update(
                formula,
                postinstall_scripts=True,
                min_release_age=0,
                min_version=SemVer("999.0.0"),
                nocache=True,
            )

        too_new = Binary(
            name=formula,
            binproviders=[
                BrewProvider(postinstall_scripts=True, min_release_age=0),
            ],
            postinstall_scripts=True,
            min_release_age=0,
            min_version=SemVer("999.0.0"),
        )
        with pytest.raises(BinaryLoadOrInstallError):
            too_new.load_or_install(nocache=True)

    def test_helper_install_args_used_by_pyinfra_ansible_backends(self, test_machine):
        test_machine.require_tool("brew")
        primary = _pick_formula_for_live_cycle()
        extra = "fzy" if primary != "fzy" else "watch"

        provider = BrewProvider(
            postinstall_scripts=True,
            min_release_age=0,
        ).get_provider_with_overrides(
            overrides={primary: {"install_args": [primary, extra]}},
        )

        for pkg in (primary, extra):
            provider.uninstall(pkg, quiet=True, nocache=True)

        installed = provider.install(primary, nocache=True)
        test_machine.assert_shallow_binary_loaded(installed)

        assert provider.load(extra, quiet=True, nocache=True) is not None

        provider.uninstall(primary, nocache=True)
        provider.uninstall(extra, quiet=True, nocache=True)

    def test_binary_direct_methods_exercise_real_lifecycle(self, test_machine):
        test_machine.require_tool("brew")
        formula = _pick_formula_for_live_cycle()
        binary = Binary(
            name=formula,
            binproviders=[
                BrewProvider(postinstall_scripts=True, min_release_age=0),
            ],
            postinstall_scripts=True,
            min_release_age=0,
        )
        test_machine.exercise_binary_lifecycle(binary)

    def test_provider_dry_run_does_not_install_formula(self, test_machine):
        test_machine.require_tool("brew")
        provider = BrewProvider(postinstall_scripts=False, min_release_age=0)
        test_machine.exercise_provider_dry_run(
            provider,
            bin_name=test_machine.pick_missing_brew_formula(),
        )
