from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from abx_pkg import SemVer
import abx_pkg.cli as cli_module


class FakeBinary:
    loaded_abspath = Path("/tmp/fake-bin")
    loaded_version = SemVer("1.2.3")
    loaded_binprovider = SimpleNamespace(name="pnpm")

    def __init__(self):
        self.calls: list[tuple[str, bool | None]] = []

    def install(self, dry_run=None):
        self.calls.append(("install", dry_run))
        return self


class FakeProvider:
    def __init__(self, name: str, abspath: str | None, version: str | None):
        self.name = name
        self.INSTALLER_BIN = name
        self.INSTALLER_BIN_ABSPATH = Path(abspath) if abspath else None
        self.INSTALLER_BINARY = (
            SimpleNamespace(loaded_version=SemVer(version))
            if abspath and version
            else None
        )


def test_build_providers_uses_managed_lib_layout(tmp_path):
    providers = cli_module.build_providers(
        ["uv", "pip", "pnpm", "cargo", "env"],
        tmp_path,
        dry_run=True,
    )

    assert providers[0].install_root == tmp_path / "uv" / "venv"
    assert providers[1].install_root == tmp_path / "pip" / "venv"
    assert providers[2].install_root == tmp_path / "pnpm"
    assert providers[3].install_root == tmp_path / "cargo"
    assert providers[4].name == "env"
    assert all(provider.dry_run for provider in providers)


def test_install_command_uses_env_defaults(monkeypatch, tmp_path):
    captured = {}
    fake_binary = FakeBinary()

    def fake_build_binary(binary_name, options, *, dry_run):
        captured["binary_name"] = binary_name
        captured["options"] = options
        captured["provider_dry_run"] = dry_run
        return fake_binary

    monkeypatch.setattr(cli_module, "build_binary", fake_build_binary)

    result = CliRunner().invoke(
        cli_module.cli,
        ["install", "prettier"],
        env={
            "ABX_PKG_LIB_DIR": str(tmp_path),
            "ABX_PKG_BINPROVIDERS": "pnpm,uv",
            "ABX_PKG_DRY_RUN": "1",
        },
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "prettier"
    assert captured["options"].lib_dir == tmp_path.resolve()
    assert captured["options"].provider_names == ["pnpm", "uv"]
    assert captured["options"].dry_run is True
    assert captured["provider_dry_run"] is True
    assert fake_binary.calls == [("install", True)]


def test_version_flag_and_command_render_same_report(monkeypatch, tmp_path):
    fake_providers = [
        FakeProvider("env", "/usr/bin/which", "2.0.0"),
        FakeProvider("uv", "/opt/homebrew/bin/uv", "0.7.1"),
        FakeProvider("apt", None, None),
    ]

    monkeypatch.setattr(
        cli_module,
        "build_providers",
        lambda *args, **kwargs: fake_providers,
    )

    runner = CliRunner()
    flag_result = runner.invoke(
        cli_module.cli,
        ["--version", f"--lib={tmp_path}", "--binproviders=env,uv,apt"],
    )
    command_result = runner.invoke(
        cli_module.cli,
        ["version", f"--lib={tmp_path}", "--binproviders=env,uv,apt"],
    )

    assert flag_result.exit_code == 0
    assert command_result.exit_code == 0
    assert flag_result.output == command_result.output

    lines = flag_result.output.strip().splitlines()
    assert lines[0] == cli_module.get_package_version()
    assert lines[1] == "env env /usr/bin/which 2.0.0"
    assert lines[2] == "uv uv /opt/homebrew/bin/uv 0.7.1"


def test_configure_cli_logging_uses_debug_level_for_interactive_tty(monkeypatch):
    captured = {}

    monkeypatch.setattr(cli_module, "is_interactive_tty", lambda: True)

    def fake_configure_logging(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_module, "configure_logging", fake_configure_logging)

    cli_module.configure_cli_logging(dry_run=False)

    assert captured["level"] == "DEBUG"
    assert captured["handler"].stream is cli_module.sys.stderr
    assert captured["fmt"] == "%(message)s"
    assert captured["replace_handlers"] is True
