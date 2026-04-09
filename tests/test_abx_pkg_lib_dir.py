"""End-to-end coverage for the ``ABX_PKG_LIB_DIR`` env var.

Because the constant is read once at module import time in
``abx_pkg.base_types``, every assertion has to run inside a fresh
subprocess with the env var pre-set; that matches how a user would
actually invoke abx-pkg from their own process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from abx_pkg import ALL_PROVIDERS


def _run_with_lib_dir(
    lib_dir_value: str,
    script: str,
    *,
    extra_env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ABX_PKG_LIB_DIR"] = lib_dir_value
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


def _providers_with_install_root_field() -> list[tuple[str, str]]:
    """``(provider_name, install_root_field)`` for every wired provider."""
    return [
        (provider_cls.model_fields["name"].default, provider_cls.INSTALL_ROOT_FIELD)
        for provider_cls in ALL_PROVIDERS
        if provider_cls.INSTALL_ROOT_FIELD
    ]


class TestAbxPkgLibDir:
    def test_unset_leaves_library_constant_none(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; os.environ.pop('ABX_PKG_LIB_DIR', None);"
                "from abx_pkg.base_types import ABX_PKG_LIB_DIR;"
                "print(ABX_PKG_LIB_DIR)",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "None"

    def test_empty_string_is_treated_as_unset(self):
        proc = _run_with_lib_dir(
            "",
            "from abx_pkg.base_types import ABX_PKG_LIB_DIR; print(ABX_PKG_LIB_DIR)",
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "None"

    @pytest.mark.parametrize(
        "lib_dir_value",
        [
            "./lib",
            "~/.config/abx/lib",
            "/tmp/abxlib",
        ],
    )
    def test_all_path_formats_resolve_across_every_provider(
        self,
        lib_dir_value,
        tmp_path,
    ):
        script = textwrap.dedent(
            """
            import json
            from pathlib import Path
            from abx_pkg.base_types import ABX_PKG_LIB_DIR
            from abx_pkg import ALL_PROVIDERS

            payload = {
                "lib_dir": str(ABX_PKG_LIB_DIR) if ABX_PKG_LIB_DIR else None,
                "fields": {},
            }
            for cls in ALL_PROVIDERS:
                field = cls.INSTALL_ROOT_FIELD
                if not field:
                    continue
                instance = cls()
                value = getattr(instance, field)
                payload["fields"][instance.name] = (
                    str(value) if value is not None else None
                )
            print(json.dumps(payload))
            """,
        )

        proc = _run_with_lib_dir(lib_dir_value, script, cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])

        resolved_lib_dir = Path(payload["lib_dir"])
        assert resolved_lib_dir.is_absolute(), (
            f"ABX_PKG_LIB_DIR={lib_dir_value!r} did not resolve to an absolute "
            f"path; got {resolved_lib_dir}"
        )

        if lib_dir_value == "./lib":
            assert resolved_lib_dir == (tmp_path / "lib").resolve()
        elif lib_dir_value == "~/.config/abx/lib":
            assert resolved_lib_dir == Path("~/.config/abx/lib").expanduser().resolve()
        else:
            assert resolved_lib_dir == Path(lib_dir_value).resolve()

        # Every provider with an install_root field now defaults into
        # ``<lib_dir>/<provider_name>`` — with no exceptions. Any
        # provider whose ``@model_validator`` auto-corrects the field
        # after construction (historically BrewProvider did this) must
        # teach the validator to respect the caller's explicit value.
        expected_names = {
            provider_name for provider_name, _ in _providers_with_install_root_field()
        }
        assert set(payload["fields"]) == expected_names

        for provider_name, field_value in payload["fields"].items():
            assert field_value is not None, (
                f"{provider_name}: INSTALL_ROOT_FIELD came back None even "
                f"though ABX_PKG_LIB_DIR={lib_dir_value!r} was set"
            )
            assert Path(field_value) == resolved_lib_dir / provider_name, (
                f"{provider_name}: expected {resolved_lib_dir / provider_name}, "
                f"got {field_value} (a model_validator somewhere is "
                f"overriding the ABX_PKG_LIB_DIR default)"
            )

    def test_explicit_install_root_kwarg_overrides_env_var(self, tmp_path):
        explicit_root = tmp_path / "explicit-override"
        script = textwrap.dedent(
            f"""
            import json
            from pathlib import Path
            from abx_pkg import (
                CargoProvider, DenoProvider, NpmProvider, PipProvider,
                PlaywrightProvider, PuppeteerProvider, UvProvider,
            )
            explicit = Path({str(explicit_root)!r})
            payload = {{
                "npm": str(NpmProvider(install_root=explicit).npm_prefix),
                "pip": str(PipProvider(install_root=explicit).pip_venv),
                "uv":  str(UvProvider(install_root=explicit).uv_venv),
                "cargo": str(CargoProvider(install_root=explicit).cargo_root),
                "deno": str(DenoProvider(install_root=explicit).deno_root),
                "puppeteer": str(
                    PuppeteerProvider(install_root=explicit).puppeteer_root,
                ),
                "playwright": str(
                    PlaywrightProvider(install_root=explicit).playwright_root,
                ),
            }}
            print(json.dumps(payload))
            """,
        )
        proc = _run_with_lib_dir("/tmp/should-be-ignored", script)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        for provider_name, value in payload.items():
            assert Path(value) == explicit_root, (
                f"{provider_name}: explicit install_root kwarg did not override "
                f"ABX_PKG_LIB_DIR; got {value}"
            )

    def test_provider_specific_alias_kwarg_overrides_env_var(self, tmp_path):
        """Passing the provider-specific alias (``npm_prefix=...``) also wins."""
        explicit_npm = tmp_path / "custom-npm"
        explicit_uv = tmp_path / "custom-uv"
        script = textwrap.dedent(
            f"""
            import json
            from pathlib import Path
            from abx_pkg import NpmProvider, UvProvider
            print(json.dumps({{
                "npm": str(NpmProvider(npm_prefix=Path({str(explicit_npm)!r})).npm_prefix),
                "uv": str(UvProvider(uv_venv=Path({str(explicit_uv)!r})).uv_venv),
            }}))
            """,
        )
        proc = _run_with_lib_dir("/tmp/should-be-ignored", script)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert Path(payload["npm"]) == explicit_npm
        assert Path(payload["uv"]) == explicit_uv

    def test_per_provider_root_env_var_overrides_abx_pkg_lib_dir(self, tmp_path):
        """``ABX_PKG_<NAME>_ROOT`` beats ``ABX_PKG_LIB_DIR`` for that provider.

        Asserts the precedence rule across every provider that ships
        an install-root field — set both env vars and confirm the
        per-provider one wins for the targeted provider while the
        ``ABX_PKG_LIB_DIR`` default still applies to the rest.
        """
        lib_dir = tmp_path / "lib"
        per_provider_dirs = {
            name: tmp_path / f"custom-{name}"
            for name, _ in _providers_with_install_root_field()
        }
        env_overrides = {
            f"ABX_PKG_{name.upper()}_ROOT": str(path)
            for name, path in per_provider_dirs.items()
        }

        script = textwrap.dedent(
            """
            import json
            from abx_pkg import ALL_PROVIDERS
            payload = {}
            for cls in ALL_PROVIDERS:
                field = cls.INSTALL_ROOT_FIELD
                if not field:
                    continue
                instance = cls()
                value = getattr(instance, field)
                payload[instance.name] = str(value) if value is not None else None
            print(json.dumps(payload))
            """,
        )
        proc = _run_with_lib_dir(
            str(lib_dir),
            script,
            extra_env=env_overrides,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])

        for name, expected in per_provider_dirs.items():
            actual = payload.get(name)
            assert actual is not None, (
                f"{name}: INSTALL_ROOT_FIELD came back None even though "
                f"ABX_PKG_{name.upper()}_ROOT={expected} was set"
            )
            assert Path(actual) == expected.resolve(), (
                f"{name}: ABX_PKG_{name.upper()}_ROOT did not win over "
                f"ABX_PKG_LIB_DIR; expected {expected.resolve()}, got {actual}"
            )

    def test_per_provider_root_alone_resolves_correctly(self, tmp_path):
        """``ABX_PKG_<NAME>_ROOT`` works even with ``ABX_PKG_LIB_DIR`` unset.

        And providers without a per-provider env var set still use
        their built-in defaults (``None`` for the nullable ones).
        """
        explicit_npm = tmp_path / "npm-only"
        script = textwrap.dedent(
            """
            import json
            from abx_pkg import NpmProvider, PipProvider
            print(json.dumps({
                "npm": str(NpmProvider().npm_prefix),
                "pip": str(PipProvider().pip_venv) if PipProvider().pip_venv else None,
            }))
            """,
        )
        env = os.environ.copy()
        env.pop("ABX_PKG_LIB_DIR", None)
        env["ABX_PKG_NPM_ROOT"] = str(explicit_npm)
        env.pop("ABX_PKG_PIP_ROOT", None)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert Path(payload["npm"]) == explicit_npm.resolve()
        assert payload["pip"] is None

    def test_real_installs_land_under_abx_pkg_lib_dir(self, test_machine):
        """End-to-end: set ``ABX_PKG_LIB_DIR`` and run real installs through
        every fast package manager; assert that each provider's expected
        subdirectory appears on disk inside the lib dir afterwards.

        Excludes the slow browser installers (puppeteer/playwright) and
        tools that can't be rehomed on the fly (brew, nix, goget,
        chromewebstore, docker, bash).
        """
        test_machine.require_tool("node")
        test_machine.require_tool("npm")
        test_machine.require_tool("uv")
        test_machine.require_tool("pnpm")
        test_machine.require_tool("yarn")
        test_machine.require_tool("bun")
        test_machine.require_tool("deno")
        test_machine.require_tool("cargo")
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as tmp_dir:
            lib_dir = Path(tmp_dir) / "abx-lib"

            script = textwrap.dedent(
                """
                import json
                from pathlib import Path
                from abx_pkg import (
                    BunProvider, CargoProvider, DenoProvider, GemProvider,
                    NpmProvider, PipProvider, PnpmProvider, UvProvider,
                    YarnProvider,
                )
                from abx_pkg.base_types import ABX_PKG_LIB_DIR
                results = {"lib_dir": str(ABX_PKG_LIB_DIR)}

                # pip: installs a CLI tool into a fresh venv at pip_venv
                pip = PipProvider(postinstall_scripts=True, min_release_age=0)
                results["pip_venv"] = str(pip.pip_venv)
                pip.install("cowsay")

                # uv: same idea, into uv_venv
                uv = UvProvider(postinstall_scripts=True, min_release_age=0)
                results["uv_venv"] = str(uv.uv_venv)
                uv.install("cowsay")

                # npm: installs a global-style package under npm_prefix
                npm = NpmProvider(postinstall_scripts=True, min_release_age=0)
                results["npm_prefix"] = str(npm.npm_prefix)
                npm.install("cowsay")

                # pnpm
                pnpm = PnpmProvider(postinstall_scripts=True, min_release_age=0)
                results["pnpm_prefix"] = str(pnpm.pnpm_prefix)
                pnpm.install("cowsay")

                # yarn (Yarn 4 workspace)
                yarn = YarnProvider(postinstall_scripts=True, min_release_age=0)
                results["yarn_prefix"] = str(yarn.yarn_prefix)
                yarn.install("cowsay")

                # bun
                bun = BunProvider(postinstall_scripts=True, min_release_age=0)
                results["bun_prefix"] = str(bun.bun_prefix)
                bun.install("cowsay")

                # deno
                deno = DenoProvider(
                    postinstall_scripts=True, min_release_age=0,
                )
                results["deno_root"] = str(deno.deno_root)
                deno.install("cowsay")

                # cargo (small, fast crate)
                cargo = CargoProvider()
                results["cargo_root"] = str(cargo.cargo_root)
                cargo.get_provider_with_overrides(
                    overrides={"loc": {"install_args": ["loc"]}},
                ).install("loc")

                # gem
                gem = GemProvider()
                results["gem_home"] = str(gem.gem_home)
                gem.get_provider_with_overrides(
                    overrides={"lolcat": {"install_args": ["lolcat"]}},
                ).install("lolcat")

                print(json.dumps(results))
                """,
            )

            proc = _run_with_lib_dir(str(lib_dir), script)
            assert proc.returncode == 0, (
                f"Real-install script failed under ABX_PKG_LIB_DIR={lib_dir}:\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
            payload = json.loads(proc.stdout.strip().splitlines()[-1])

            assert Path(payload["lib_dir"]) == lib_dir.resolve()

            # Every provider's install root should live inside lib_dir.
            for key, subdir_name in (
                ("pip_venv", "pip"),
                ("uv_venv", "uv"),
                ("npm_prefix", "npm"),
                ("pnpm_prefix", "pnpm"),
                ("yarn_prefix", "yarn"),
                ("bun_prefix", "bun"),
                ("deno_root", "deno"),
                ("cargo_root", "cargo"),
                ("gem_home", "gem"),
            ):
                reported = Path(payload[key])
                assert reported == lib_dir.resolve() / subdir_name, (
                    f"{key}: expected {lib_dir.resolve() / subdir_name}, got {reported}"
                )
                assert reported.exists(), (
                    f"{key}: {reported} does not exist on disk after real install"
                )
                assert reported.is_dir(), (
                    f"{key}: {reported} exists but is not a directory"
                )

            # Spot-check that every expected top-level subdir landed in place.
            top_level_subdirs = {
                child.name for child in lib_dir.iterdir() if child.is_dir()
            }
            assert {
                "pip",
                "uv",
                "npm",
                "pnpm",
                "yarn",
                "bun",
                "deno",
                "cargo",
                "gem",
            }.issubset(top_level_subdirs), (
                f"Missing expected subdirs under {lib_dir}; saw {top_level_subdirs}"
            )
