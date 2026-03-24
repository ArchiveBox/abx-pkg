#!/usr/bin/env python

import os
import sys
import inspect
import shutil
import tempfile
import unittest
import subprocess
import contextlib
import time
import logging
from io import StringIO
from unittest import mock
from pathlib import Path
from typing import Optional
from types import SimpleNamespace

# from rich import print

from abx_pkg import (
    BinName,
    BinProvider,
    EnvProvider,
    Binary,
    SemVer,
    BinProviderOverrides,
    HandlerDict,
    HostBinPath,
    InstallArgs,
    PipProvider,
    NpmProvider,
    AptProvider,
    BrewProvider,
    CargoProvider,
    GemProvider,
    GoGetProvider,
    NixProvider,
    DockerProvider,
    configure_logging,
    configure_rich_logging,
    get_logger,
    RICH_INSTALLED,
    BinaryInstallError,
)
from abx_pkg.binprovider import remap_kwargs
from abx_pkg.binprovider_ansible import AnsibleProvider
from abx_pkg.binprovider_pyinfra import PyinfraProvider
from abx_pkg.logging import summarize_value

REAL_OS_STAT = os.stat


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def capture_abx_logs(level: int | str):
    package_logger = get_logger()
    handler = ListHandler()
    original_handlers = list(package_logger.handlers)
    original_level = package_logger.level
    original_propagate = package_logger.propagate

    configure_logging(
        level=level,
        handler=handler,
        replace_handlers=True,
        propagate=False,
    )

    try:
        yield handler.records
    finally:
        package_logger.handlers.clear()
        package_logger.handlers.extend(original_handlers)
        package_logger.setLevel(original_level)
        package_logger.propagate = original_propagate


def stat_with_uid(path, uid):
    result = REAL_OS_STAT(path)
    return os.stat_result(
        (
            result.st_mode,
            result.st_ino,
            result.st_dev,
            result.st_nlink,
            uid,
            result.st_gid,
            result.st_size,
            int(result.st_atime),
            int(result.st_mtime),
            int(result.st_ctime),
        ),
    )


class TestSemVer(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(SemVer(None), None)
        self.assertEqual(SemVer(""), None)
        self.assertEqual(SemVer.parse(""), None)
        self.assertEqual(SemVer(1), (1, 0, 0))
        self.assertEqual(SemVer(1, 2), (1, 2, 0))
        self.assertEqual(SemVer("1.2+234234"), (1, 2, 234234))
        self.assertEqual(SemVer("1.2+beta"), (1, 2, 0))
        self.assertEqual(SemVer("1.2.4(1)+beta"), (1, 2, 4))
        self.assertEqual(SemVer("1.2+beta(3)"), (1, 2, 3))
        self.assertEqual(SemVer("1.2+6-be1ta(4)"), (1, 2, 6))
        self.assertEqual(SemVer("1.2 curl(8)beta-4"), (1, 2, 0))
        self.assertEqual(SemVer("1.2+curl(8)beta-4"), (1, 2, 8))
        self.assertEqual(SemVer((1, 2, 3)), (1, 2, 3))
        self.assertEqual(getattr(SemVer((1, 2, 3)), "full_text"), "1.2.3")
        self.assertEqual(SemVer(("1", "2", "3")), (1, 2, 3))
        self.assertEqual(SemVer.parse("5.6.7"), (5, 6, 7))
        self.assertEqual(SemVer.parse("124.0.6367.208"), (124, 0, 6367))
        self.assertEqual(SemVer.parse("Google Chrome 124.1+234.234"), (124, 1, 234))
        self.assertEqual(SemVer.parse("Google Ch1rome 124.0.6367.208"), (124, 0, 6367))
        self.assertEqual(
            SemVer.parse(
                "Google Chrome 124.0.6367.208+beta_234. 234.234.123\n123.456.324",
            ),
            (124, 0, 6367),
        )
        self.assertEqual(
            getattr(
                SemVer.parse(
                    "Google Chrome 124.0.6367.208+beta_234. 234.234.123\n123.456.324",
                ),
                "full_text",
            ),
            "Google Chrome 124.0.6367.208+beta_234. 234.234.123",
        )
        self.assertEqual(SemVer.parse("Google Chrome"), None)


class TestLogging(unittest.TestCase):
    def test_configure_logging_accepts_string_level(self):
        with capture_abx_logs("INFO") as records:
            logger = get_logger()
            logger.info("hello from tests")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].levelno, logging.INFO)
        self.assertEqual(records[0].getMessage(), "hello from tests")

    def test_debug_logging_emits_method_calls(self):
        binary = Binary(name="python", binproviders=[EnvProvider()])

        with capture_abx_logs(logging.DEBUG) as records:
            binary.load()

        messages = [record.getMessage() for record in records]
        self.assertTrue(any(message.startswith("Binary.load(") for message in messages))
        self.assertTrue(
            any(message.startswith("BinProvider.load(") for message in messages),
        )
        self.assertFalse(
            any("._call_handler_for_action(" in message for message in messages),
        )
        self.assertFalse(
            any("._get_handler_for_action(" in message for message in messages),
        )

    def test_info_logging_emits_lifecycle_messages_without_debug_calls(self):
        binary = Binary(name="python", binproviders=[EnvProvider()])

        with capture_abx_logs(logging.INFO) as records:
            binary.load()

        messages = [record.getMessage() for record in records]
        self.assertTrue(any("Loading python binary" in message for message in messages))
        loaded_messages = [
            message
            for message in messages
            if message.startswith("Loaded ") and " via EnvProvider()" in message
        ]
        self.assertEqual(len(loaded_messages), 1)
        self.assertFalse(any(message.startswith("Calling ") for message in messages))

    def test_warning_logging_emits_failures(self):
        class BrokenProvider(BinProvider):
            name: str = "broken"

            def default_install_handler(
                self,
                bin_name: str,
                install_args: InstallArgs | None = None,
                **context,
            ) -> str:
                raise RuntimeError("boom")

        binary = Binary(name="missing-bin", binproviders=[BrokenProvider()])

        with capture_abx_logs(logging.WARNING) as records:
            with self.assertRaises(Exception):
                binary.install()

        messages = [record.getMessage() for record in records]
        self.assertTrue(
            any(
                message.startswith("Binary.install(")
                and " raised BinaryInstallError(" in message
                for message in messages
            ),
        )
        self.assertFalse(any(record.levelno < logging.WARNING for record in records))

    def test_error_logging_omits_redundant_binary_wrapper_warning(self):
        class BrokenProvider(BinProvider):
            name: str = "broken"

            def default_install_handler(
                self,
                bin_name: str,
                install_args: InstallArgs | None = None,
                **context,
            ):
                raise RuntimeError("simulated install failure for trace output")

        binary = Binary(
            name="definitely-missing-abx-pkg-bin",
            binproviders=[EnvProvider(), BrokenProvider()],
        )

        with capture_abx_logs(logging.ERROR) as records:
            with self.assertRaises(BinaryInstallError):
                binary.install()

        messages = [record.getMessage() for record in records]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("Binary.install("))
        self.assertIn(" raised BinaryInstallError(", messages[0])
        self.assertIn("definitely-missing-abx-pkg-bin", messages[0])
        self.assertIn("ERRORS=", messages[0])
        self.assertFalse(any(record.levelno == logging.WARNING for record in records))

    def test_summarize_value_handles_unreprable_objects(self):
        class BrokenValue:
            def __repr__(self) -> str:
                raise RuntimeError("boom")

        self.assertEqual(summarize_value(BrokenValue()), "BrokenValue(...)")

    def test_load_or_install_does_not_log_error_for_expected_load_fallback(self):
        class FallbackProvider(BinProvider):
            name: str = "fallback"
            installed: bool = False

            def default_abspath_handler(
                self,
                bin_name: BinName | HostBinPath,
                **context,
            ):
                if not self.installed:
                    return None
                return Path("/usr/bin/true")

            def default_version_handler(
                self,
                bin_name: str,
                abspath: Path | None = None,
                **context,
            ):
                if not self.installed:
                    return None
                return SemVer("1.2.3")

            def default_install_handler(
                self,
                bin_name: str,
                install_args: InstallArgs | None = None,
                **context,
            ):
                self.installed = True
                return "installed"

        provider = FallbackProvider()

        with capture_abx_logs(logging.DEBUG) as records:
            result = provider.load_or_install("missing-bin")

        self.assertIsNotNone(result)
        messages = [record.getMessage() for record in records]
        self.assertFalse(any(record.levelno >= logging.ERROR for record in records))
        self.assertFalse(any("raised RuntimeError" in message for message in messages))

    def test_multi_provider_load_or_install_logs_intermediate_failures_at_debug_only(
        self,
    ):
        class FirstFailsProvider(BinProvider):
            name: str = "first_fail"
            installed: bool = False

            def default_abspath_handler(
                self,
                bin_name: BinName | HostBinPath,
                **context,
            ):
                return None

            def default_install_handler(
                self,
                bin_name: str,
                install_args: InstallArgs | None = None,
                **context,
            ):
                raise RuntimeError("simulated first provider failure")

        class SecondSucceedsProvider(BinProvider):
            name: str = "second_ok"
            installed: bool = False

            def default_abspath_handler(
                self,
                bin_name: BinName | HostBinPath,
                **context,
            ):
                if not self.installed:
                    return None
                return Path("/usr/bin/true")

            def default_version_handler(
                self,
                bin_name: str,
                abspath: Path | None = None,
                **context,
            ):
                if not self.installed:
                    return None
                return SemVer("2.3.4")

            def default_install_handler(
                self,
                bin_name: str,
                install_args: InstallArgs | None = None,
                **context,
            ):
                self.installed = True
                return "installed"

        binary = Binary(
            name="demo-multi-provider-ok",
            binproviders=[FirstFailsProvider(), SecondSucceedsProvider()],
        )

        with capture_abx_logs(logging.DEBUG) as records:
            result = binary.load_or_install()

        self.assertIsNotNone(result)
        messages = [record.getMessage() for record in records]
        self.assertTrue(
            any(
                "FirstFailsProvider.load_or_install(" in message
                and "raised RuntimeError(" in message
                for message in messages
            ),
        )
        self.assertFalse(any(record.levelno >= logging.ERROR for record in records))

    def test_multi_provider_load_or_install_failure_error_is_not_truncated(self):
        class FirstFailsProvider(BinProvider):
            name: str = "first_fail"

            def default_abspath_handler(
                self,
                bin_name: BinName | HostBinPath,
                **context,
            ):
                return None

            def default_install_handler(
                self,
                bin_name: str,
                install_args: InstallArgs | None = None,
                **context,
            ):
                raise RuntimeError("simulated first provider failure")

        class ThirdFailsProvider(BinProvider):
            name: str = "third_fail"

            def default_abspath_handler(
                self,
                bin_name: BinName | HostBinPath,
                **context,
            ):
                return None

            def default_install_handler(
                self,
                bin_name: str,
                install_args: InstallArgs | None = None,
                **context,
            ):
                raise RuntimeError(
                    "simulated third provider failure with enough text to catch truncation",
                )

        binary = Binary(
            name="demo-multi-provider-bad",
            binproviders=[FirstFailsProvider(), ThirdFailsProvider()],
        )

        with capture_abx_logs(logging.ERROR) as records:
            with self.assertRaises(Exception):
                binary.load_or_install()

        message = records[0].getMessage()
        self.assertIn("BinaryLoadOrInstallError(", message)
        self.assertIn("first_fail", message)
        self.assertIn("third_fail", message)
        self.assertIn(
            "simulated third provider failure with enough text to catch truncation",
            message,
        )
        self.assertNotIn("...", message.split("raised ", 1)[-1])

    def test_configure_rich_logging_if_available(self):
        self.assertTrue(RICH_INSTALLED, "rich not installed")
        from rich.console import Console

        stream = StringIO()
        console = Console(
            file=stream,
            force_terminal=True,
            color_system="standard",
            width=120,
        )

        package_logger = get_logger()
        original_handlers = list(package_logger.handlers)
        original_level = package_logger.level
        original_propagate = package_logger.propagate

        try:
            configure_rich_logging(
                logging.INFO,
                console=console,
                replace_handlers=True,
                show_path=False,
                show_time=False,
            )
            self.assertEqual(
                package_logger.handlers[0].__class__.__name__,
                "RichHandler",
            )
            get_logger().info("hello rich")
        finally:
            package_logger.handlers.clear()
            package_logger.handlers.extend(original_handlers)
            package_logger.setLevel(original_level)
            package_logger.propagate = original_propagate

        output = stream.getvalue()
        self.assertIn("hello rich", output)
        self.assertIn("INFO", output)


class TestBinProvider(unittest.TestCase):
    def test_python_env(self):
        provider = EnvProvider()

        python_bin = provider.load("python")
        assert python_bin is not None
        self.assertEqual(python_bin, provider.load_or_install("python"))

        self.assertEqual(
            python_bin.loaded_version,
            SemVer("{}.{}.{}".format(*sys.version_info[:3])),
        )
        self.assertEqual(python_bin.loaded_abspath, Path(sys.executable).absolute())
        self.assertEqual(python_bin.loaded_respath, Path(sys.executable).resolve())
        self.assertTrue(python_bin.is_valid)
        self.assertTrue(python_bin.is_executable)
        self.assertFalse(python_bin.is_script)
        self.assertTrue(
            bool(str(python_bin)),
        )  # easy way to make sure serializing doesn't throw an error
        assert python_bin.loaded_binprovider is not None
        installer_binary = python_bin.loaded_binprovider.INSTALLER_BINARY
        assert installer_binary is not None
        self.assertEqual(
            str(installer_binary.loaded_abspath),
            str(shutil.which("which")),
        )

    def test_bash_env(self):
        envprovider = EnvProvider()

        SYS_BASH_VERSION = subprocess.check_output(
            "bash --version",
            shell=True,
            text=True,
        ).split("\n")[0]

        bash_bin = envprovider.load_or_install("bash")
        assert bash_bin is not None
        bash_version = bash_bin.loaded_version
        assert bash_version is not None
        bash_abspath = shutil.which("bash")
        assert bash_abspath is not None
        minimum_bash_version = SemVer.parse("3.0.0")
        assert minimum_bash_version is not None
        self.assertEqual(bash_version, SemVer(SYS_BASH_VERSION))
        self.assertTrue(tuple(bash_version) > tuple(minimum_bash_version))
        self.assertEqual(bash_bin.loaded_abspath, Path(bash_abspath))
        self.assertTrue(bash_bin.is_valid)
        self.assertTrue(bash_bin.is_executable)
        self.assertFalse(bash_bin.is_script)
        self.assertTrue(
            bool(str(bash_bin)),
        )  # easy way to make sure serializing doesn't throw an error

    def test_overrides(self):

        class TestRecord:
            called_default_abspath_getter = False
            called_default_version_getter = False
            called_default_packages_getter = False
            called_custom_install_handler = False
            received_legacy_install_packages = None
            received_new_install_args = None

        def custom_version_getter():
            return "1.2.3"

        def custom_abspath_getter(
            binprovider: BinProvider,
            bin_name: BinName,
            **context,
        ):
            assert binprovider.__class__.__name__ == "CustomProvider"
            return "/usr/bin/true"

        def legacy_install_handler(
            binprovider: BinProvider,
            bin_name: BinName,
            **context,
        ):
            packages = context.get("packages")
            TestRecord.called_custom_install_handler = True
            TestRecord.received_legacy_install_packages = packages
            return "legacy install ok"

        def new_install_handler(
            binprovider: BinProvider,
            bin_name: BinName,
            **context,
        ):
            install_args = context.get("install_args")
            TestRecord.received_new_install_args = install_args
            return "new install ok"

        default_handlers: HandlerDict = {
            "abspath": "self.default_abspath_getter",
            "install_args": "self.default_packages_getter",
            "version": "self.default_version_getter",
            "install": None,
        }
        somebin_handlers: HandlerDict = {
            "abspath": custom_abspath_getter,
            "version": custom_version_getter,
            "packages": ["literal", "return", "value"],
        }
        abc_handlers: HandlerDict = {
            "install_args": "self.alternate_packages_getter",
        }
        legacy_install_handlers: HandlerDict = {
            "packages": ["legacy-pkg"],
            "install": legacy_install_handler,
        }
        new_install_handlers: HandlerDict = {
            "install_args": ["new-pkg"],
            "install": new_install_handler,
        }

        class CustomProvider(BinProvider):
            name: str = "custom"

            overrides: BinProviderOverrides = {
                "*": default_handlers,
                "somebin": somebin_handlers,
                "abc": abc_handlers,
                "legacyinstall": legacy_install_handlers,
                "newinstall": new_install_handlers,
            }

            @staticmethod
            def default_abspath_getter():
                TestRecord.called_default_abspath_getter = True
                return "/bin/bash"

            @classmethod
            def default_packages_getter(cls, bin_name: str, **context):
                TestRecord.called_default_packages_getter = True
                return None

            def default_version_getter(self, bin_name: str, **context):
                TestRecord.called_default_version_getter = True
                return "999.999.999"

            @classmethod
            def alternate_packages_getter(cls, bin_name: str, **context):
                TestRecord.called_default_packages_getter = True
                return ["abc", "def"]

            def on_install(self, bin_name: str, **context):
                raise NotImplementedError("whattt")

        provider = CustomProvider()
        provider._dry_run = True

        self.assertFalse(TestRecord.called_default_abspath_getter)
        self.assertFalse(TestRecord.called_default_version_getter)
        self.assertFalse(TestRecord.called_default_packages_getter)
        self.assertFalse(TestRecord.called_custom_install_handler)

        # test default abspath getter
        self.assertEqual(provider.get_abspath("doesnotexist"), Path("/bin/bash"))
        self.assertTrue(TestRecord.called_default_abspath_getter)

        # test custom abspath getter
        self.assertEqual(
            provider.get_abspath("somebin"),
            Path("/usr/bin/true"),
        )  # test that Callable getter that takes self, bin_name, **context works + result is auto-cast to Path

        # test default version getter
        self.assertEqual(
            provider.get_version("doesnotexist"),
            SemVer("999.999.999"),
        )  # test that normal 'self.some_method' dot referenced getter works and result is auto-cast to SemVer
        self.assertTrue(TestRecord.called_default_version_getter)

        # test custom version getter
        self.assertEqual(
            provider.get_version("somebin"),
            SemVer("1.2.3"),
        )  # test that remote Callable func getter that takes no args works and str result is auto-cast to SemVer

        # test default install_args getter
        self.assertEqual(
            provider.get_install_args("doesnotexist"),
            ("doesnotexist",),
        )  # test that it fallsback to [bin_name] by default if getter returns None
        self.assertTrue(TestRecord.called_default_packages_getter)
        self.assertEqual(
            provider.get_install_args("abc"),
            ("abc", "def"),
        )  # test that classmethod getter funcs work
        self.assertEqual(
            provider.get_packages("abc"),
            ("abc", "def"),
        )  # legacy getter remains as an alias

        # test custom install_args getter
        self.assertEqual(
            provider.get_install_args("somebin"),
            ("literal", "return", "value"),
        )  # test that literal return values in overrides work
        self.assertEqual(
            provider.get_packages("somebin"),
            ("literal", "return", "value"),
        )  # legacy getter still resolves legacy override keys

        # test install handler
        exc = None
        try:
            provider.install("doesnotexist")
        except Exception as err:
            exc = err
        self.assertIsInstance(exc, AssertionError)
        self.assertTrue(
            "BinProvider(name=custom) has no install handler implemented for Binary(name=doesnotexist)"
            in str(exc),
        )

        provider.install("legacyinstall")
        provider.install("newinstall")
        self.assertTrue(TestRecord.called_custom_install_handler)
        self.assertEqual(TestRecord.received_legacy_install_packages, ("legacy-pkg",))
        self.assertEqual(TestRecord.received_new_install_args, ("new-pkg",))

    def test_remap_kwargs_supports_old_and_new_names_without_changing_signature(self):
        class Example:
            @remap_kwargs({"packages": "install_args"})
            def handler(
                self,
                install_args: InstallArgs | None = None,
            ) -> InstallArgs | None:
                return install_args

        example = Example()

        self.assertEqual(example.handler(install_args=("new-style",)), ("new-style",))
        self.assertEqual(example.handler(packages=("old-style",)), ("old-style",))

        signature = inspect.signature(Example.handler)
        self.assertIn("install_args", signature.parameters)
        self.assertNotIn("packages", signature.parameters)
        self.assertEqual(
            signature.parameters["install_args"].annotation,
            Optional[InstallArgs],
        )
        self.assertEqual(signature.return_annotation, InstallArgs | None)

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/custom/prefix/bin/brew"),
    )
    @mock.patch("abx_pkg.binprovider_brew.os.access", return_value=False)
    @mock.patch("abx_pkg.binprovider_brew.os.path.isdir", return_value=False)
    def test_brew_provider_load_path_uses_installer_prefix_without_shelling_out(
        self,
        _mock_isdir,
        _mock_access,
        _mock_installer_bin_abspath,
    ):
        with mock.patch.object(BrewProvider, "exec") as mock_exec:
            provider = BrewProvider()

        self.assertEqual(provider.brew_prefix, Path("/custom/prefix"))
        self.assertEqual(provider.PATH, "/custom/prefix/bin")
        mock_exec.assert_not_called()

    def test_brew_provider_resolves_formula_binary_from_active_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            active_prefix = tmpdir_path / "active"
            stale_prefix = tmpdir_path / "stale"
            active_brew = active_prefix / "bin" / "brew"
            active_java = active_prefix / "opt" / "openjdk" / "bin" / "java"
            stale_java = stale_prefix / "opt" / "openjdk" / "bin" / "java"

            for path in (active_brew, active_java, stale_java):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n")
                path.chmod(0o755)

            (stale_prefix / "bin").mkdir(parents=True, exist_ok=True)

            provider = BrewProvider()
            provider._INSTALLER_BIN_ABSPATH = active_brew
            provider.PATH = f"{stale_prefix / 'bin'}:{active_prefix / 'bin'}"
            provider.brew_prefix = active_prefix

            with mock.patch.object(
                BrewProvider,
                "get_install_args",
                return_value=("openjdk",),
            ):
                abspath = provider.default_abspath_handler("java")

            self.assertEqual(abspath, active_java)

    def test_brew_provider_get_abspaths_includes_formula_binary_from_active_prefix(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            active_prefix = tmpdir_path / "active"
            stale_prefix = tmpdir_path / "stale"
            active_brew = active_prefix / "bin" / "brew"
            active_java = active_prefix / "opt" / "openjdk" / "bin" / "java"
            stale_java = stale_prefix / "opt" / "openjdk" / "bin" / "java"

            for path in (active_brew, active_java, stale_java):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n")
                path.chmod(0o755)

            (stale_prefix / "bin").mkdir(parents=True, exist_ok=True)

            provider = BrewProvider()
            provider._INSTALLER_BIN_ABSPATH = active_brew
            provider.PATH = f"{stale_prefix / 'bin'}:{active_prefix / 'bin'}"
            provider.brew_prefix = active_prefix

            with mock.patch.object(
                BrewProvider,
                "get_install_args",
                return_value=("openjdk",),
            ):
                abspaths = provider.get_abspaths("java", nocache=True)

            self.assertIn(active_java, abspaths)

    def test_brew_provider_resolves_formula_binary_from_openjdk_libexec_bin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            active_prefix = tmpdir_path / "active"
            active_brew = active_prefix / "bin" / "brew"
            active_java = (
                active_prefix
                / "opt"
                / "openjdk"
                / "libexec"
                / "openjdk.jdk"
                / "Contents"
                / "Home"
                / "bin"
                / "java"
            )

            for path in (active_brew, active_java):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n", encoding="utf-8")
                path.chmod(0o755)

            provider = BrewProvider()
            provider._INSTALLER_BIN_ABSPATH = active_brew
            provider.PATH = str(active_prefix / "bin")
            provider.brew_prefix = active_prefix

            with (
                mock.patch.object(
                    BrewProvider,
                    "get_install_args",
                    return_value=("openjdk",),
                ),
                mock.patch.object(
                    BrewProvider,
                    "exec",
                    return_value=subprocess.CompletedProcess(
                        args=["brew", "list", "--formula", "openjdk"],
                        returncode=0,
                        stdout=f"{active_java}\n",
                        stderr="",
                    ),
                ),
            ):
                abspath = provider.default_abspath_handler("java")

            self.assertEqual(abspath, active_java)

    def test_brew_provider_get_abspaths_includes_openjdk_libexec_bin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            active_prefix = tmpdir_path / "active"
            active_brew = active_prefix / "bin" / "brew"
            active_java = (
                active_prefix
                / "opt"
                / "openjdk"
                / "libexec"
                / "openjdk.jdk"
                / "Contents"
                / "Home"
                / "bin"
                / "java"
            )

            for path in (active_brew, active_java):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n", encoding="utf-8")
                path.chmod(0o755)

            provider = BrewProvider()
            provider._INSTALLER_BIN_ABSPATH = active_brew
            provider.PATH = str(active_prefix / "bin")
            provider.brew_prefix = active_prefix

            with (
                mock.patch.object(
                    BrewProvider,
                    "get_install_args",
                    return_value=("openjdk",),
                ),
                mock.patch.object(
                    BrewProvider,
                    "exec",
                    return_value=subprocess.CompletedProcess(
                        args=["brew", "list", "--formula", "openjdk"],
                        returncode=0,
                        stdout=f"{active_java}\n",
                        stderr="",
                    ),
                ),
            ):
                abspaths = provider.get_abspaths("java", nocache=True)

            self.assertIn(active_java, abspaths)

    @mock.patch.object(
        PipProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path(sys.executable),
    )
    @mock.patch("abx_pkg.binprovider.os.stat")
    @mock.patch("abx_pkg.binprovider.os.geteuid", return_value=0)
    @mock.patch.object(
        BinProvider,
        "uid_has_passwd_entry",
        side_effect=lambda uid: uid == 0,
    )
    def test_binprovider_euid_falls_back_from_unmapped_installer_owner(
        self,
        _mock_uid_has_passwd_entry,
        _mock_geteuid,
        mock_stat,
        _mock_installer_bin_abspath,
    ):
        class CustomProvider(BinProvider):
            name: str = "custom"

        def fake_stat(path, *args, **kwargs):
            if Path(path) == Path(sys.executable):
                return stat_with_uid(path, 1001)
            return REAL_OS_STAT(path, *args, **kwargs)

        mock_stat.side_effect = fake_stat
        provider = CustomProvider()
        self.assertEqual(provider.EUID, 0)

    @mock.patch(
        "abx_pkg.binprovider.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        ),
    )
    @mock.patch("abx_pkg.binprovider.pwd.getpwuid", side_effect=KeyError)
    @mock.patch("abx_pkg.binprovider.os.getegid", return_value=54321)
    @mock.patch("abx_pkg.binprovider.os.geteuid", return_value=12345)
    def test_exec_handles_current_uid_without_passwd_entry(
        self,
        _mock_geteuid,
        _mock_getegid,
        _mock_getpwuid,
        mock_run,
    ):
        provider = EnvProvider(euid=12345)

        with mock.patch.dict(
            os.environ,
            {
                "HOME": "/tmp/container-home",
                "USER": "container",
                "LOGNAME": "container",
            },
            clear=False,
        ):
            proc = provider.exec(bin_name=sys.executable, cmd=["--version"], quiet=True)

        self.assertEqual(proc.returncode, 0)
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["HOME"], "/tmp/container-home")
        self.assertEqual(env["USER"], "container")
        self.assertEqual(env["LOGNAME"], "container")

    @mock.patch("abx_pkg.binprovider_npm.NpmProvider._load_PATH", return_value="")
    @mock.patch("abx_pkg.binprovider.os.geteuid", return_value=0)
    def test_npm_provider_keeps_root_euid_for_global_installs(
        self,
        _mock_geteuid,
        _mock_load_path,
    ):
        provider = NpmProvider()
        self.assertEqual(provider.euid, 0)
        self.assertEqual(provider.EUID, 0)

    @mock.patch("abx_pkg.binprovider_npm.NpmProvider._load_PATH", return_value="")
    @mock.patch("abx_pkg.binprovider.os.geteuid", return_value=0)
    @mock.patch("abx_pkg.binprovider.os.stat")
    @mock.patch.object(BinProvider, "uid_has_passwd_entry", return_value=True)
    def test_npm_provider_prefers_prefix_owner_over_root(
        self,
        _mock_uid_has_passwd_entry,
        _mock_stat,
        _mock_geteuid,
        _mock_load_path,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = Path(temp_dir)
            _mock_stat.side_effect = lambda path, *args, **kwargs: (
                stat_with_uid(path, 1001)
                if Path(path) == prefix
                else REAL_OS_STAT(path, *args, **kwargs)
            )
            provider = NpmProvider(npm_prefix=prefix)
            self.assertEqual(provider.euid, 1001)

    @mock.patch("abx_pkg.binprovider.os.geteuid", return_value=0)
    def test_pip_provider_keeps_root_euid_for_global_installs(self, _mock_geteuid):
        provider = PipProvider()
        self.assertEqual(provider.euid, 0)
        self.assertEqual(provider.EUID, 0)

    def test_npm_provider_respects_explicit_euid(self):
        provider = NpmProvider(euid=0)
        self.assertEqual(provider.euid, 0)
        self.assertEqual(provider.EUID, 0)


class TestForwardRefs(unittest.TestCase):
    def test_subclass_without_overrides_import(self):
        class CustomProvider(BinProvider):
            name: str = "custom"

        provider = CustomProvider()
        self.assertEqual(provider.name, "custom")


class TestBinary(unittest.TestCase):
    def test_python_bin(self):
        envprovider = EnvProvider()

        python_bin = Binary(name="python", binproviders=[envprovider])

        self.assertIsNone(python_bin.loaded_binprovider)
        self.assertIsNone(python_bin.loaded_abspath)
        self.assertIsNone(python_bin.loaded_version)

        python_bin = python_bin.load()

        shallow_bin = envprovider.load_or_install("python")
        assert shallow_bin and python_bin.loaded_binprovider
        self.assertEqual(python_bin.loaded_binprovider, shallow_bin.loaded_binprovider)
        self.assertEqual(python_bin.loaded_abspath, shallow_bin.loaded_abspath)
        self.assertEqual(python_bin.loaded_version, shallow_bin.loaded_version)
        self.assertEqual(python_bin.loaded_sha256, shallow_bin.loaded_sha256)

        self.assertEqual(
            python_bin.loaded_version,
            SemVer("{}.{}.{}".format(*sys.version_info[:3])),
        )
        self.assertEqual(python_bin.loaded_abspath, Path(sys.executable).absolute())
        self.assertEqual(python_bin.loaded_respath, Path(sys.executable).resolve())
        self.assertTrue(python_bin.is_valid)
        self.assertTrue(python_bin.is_executable)
        self.assertFalse(python_bin.is_script)
        self.assertTrue(
            bool(str(python_bin)),
        )  # easy way to make sure serializing doesn't throw an error

    def test_repr_includes_abspath_version_and_short_sha256(self):
        envprovider = EnvProvider()

        shallow_bin = envprovider.load_or_install("python")
        assert shallow_bin is not None
        short_sha256 = f"...{str(shallow_bin.loaded_sha256)[-6:]}"

        shallow_repr = repr(shallow_bin)
        self.assertIn("ShallowBinary(", shallow_repr)
        self.assertIn(f"name={shallow_bin.name!r}", shallow_repr)
        self.assertIn(f"abspath={shallow_bin.loaded_abspath!r}", shallow_repr)
        self.assertIn(f"version={shallow_bin.loaded_version!r}", shallow_repr)
        self.assertIn(f"sha256={short_sha256!r}", shallow_repr)
        self.assertEqual(str(shallow_bin), shallow_repr)

        binary = Binary(name="python", binproviders=[envprovider]).load()
        binary_repr = repr(binary)
        self.assertIn("Binary(", binary_repr)
        self.assertIn(f"name={binary.name!r}", binary_repr)
        self.assertIn(f"abspath={binary.loaded_abspath!r}", binary_repr)
        self.assertIn(f"version={binary.loaded_version!r}", binary_repr)
        self.assertIn(f"sha256={short_sha256!r}", binary_repr)
        self.assertEqual(str(binary), binary_repr)

    def test_min_version_accepts_string(self):
        binary = Binary.model_validate(
            {
                "name": "python",
                "abspath": sys.executable,
                "version": "1.2.3",
                "min_version": "1.2.0",
            },
        )

        self.assertEqual(binary.min_version, SemVer("1.2.0"))
        self.assertTrue(binary.is_valid)

    def test_min_version_invalidates_lower_loaded_version(self):
        binary = Binary.model_validate(
            {
                "name": "python",
                "abspath": sys.executable,
                "version": "1.2.3",
                "min_version": SemVer("1.2.4"),
            },
        )

        self.assertEqual(binary.min_version, SemVer("1.2.4"))
        self.assertFalse(binary.is_valid)

    def test_min_version_allows_equal_loaded_version(self):
        binary = Binary.model_validate(
            {
                "name": "python",
                "abspath": sys.executable,
                "version": "1.2.3",
                "min_version": "1.2.3",
            },
        )

        self.assertTrue(binary.is_valid)

    def test_default_version_handler_reads_version_from_stderr(self):
        provider = BinProvider(name="mock", PATH="/tmp", INSTALLER_BIN="mock")
        proc = subprocess.CompletedProcess(
            args=["java", "-version"],
            returncode=0,
            stdout="",
            stderr='openjdk version "25.0.2" 2026-01-20\n',
        )

        with mock.patch.object(BinProvider, "exec", return_value=proc):
            version = provider.default_version_handler(
                "java",
                abspath=Path("/tmp/java"),
            )

        self.assertEqual(version, SemVer("25.0.2"))

    def test_load_or_install_skips_provider_below_min_version(self):
        env_provider = EnvProvider()
        brew_provider = BrewProvider()
        binary = Binary(
            name="java",
            min_version=SemVer("11.0.0"),
            binproviders=[env_provider, brew_provider],
        )

        env_result = SimpleNamespace(
            loaded_abspath=Path("/usr/bin/java"),
            loaded_version=SemVer("1.8.0"),
            loaded_sha256="env-sha",
        )
        brew_result = SimpleNamespace(
            loaded_abspath=Path(
                "/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home/bin/java",
            ),
            loaded_version=SemVer("25.0.2"),
            loaded_sha256="brew-sha",
        )

        with (
            mock.patch.object(
                Binary,
                "get_binprovider",
                side_effect=[env_provider, brew_provider],
            ),
            mock.patch.object(
                EnvProvider,
                "load_or_install",
                return_value=env_result,
            ),
            mock.patch.object(
                BrewProvider,
                "load_or_install",
                return_value=brew_result,
            ),
        ):
            result = binary.load_or_install()

        loaded_binprovider = result.loaded_binprovider
        self.assertIsNotNone(loaded_binprovider)
        assert loaded_binprovider is not None
        self.assertEqual(loaded_binprovider.name, "brew")
        self.assertEqual(result.loaded_version, SemVer("25.0.2"))

    def test_load_or_install_accepts_provider_when_min_version_is_none(self):
        env_provider = EnvProvider()
        brew_provider = BrewProvider()
        binary = Binary(
            name="java",
            min_version=None,
            binproviders=[env_provider, brew_provider],
        )

        env_result = SimpleNamespace(
            loaded_abspath=Path("/usr/bin/java"),
            loaded_version=SemVer("1.8.0"),
            loaded_sha256="env-sha",
        )
        brew_result = SimpleNamespace(
            loaded_abspath=Path(
                "/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home/bin/java",
            ),
            loaded_version=SemVer("25.0.2"),
            loaded_sha256="brew-sha",
        )

        with (
            mock.patch.object(
                Binary,
                "get_binprovider",
                side_effect=[env_provider, brew_provider],
            ),
            mock.patch.object(
                EnvProvider,
                "load_or_install",
                return_value=env_result,
            ),
            mock.patch.object(
                BrewProvider,
                "load_or_install",
                return_value=brew_result,
            ),
        ):
            result = binary.load_or_install()

        loaded_binprovider = result.loaded_binprovider
        self.assertIsNotNone(loaded_binprovider)
        assert loaded_binprovider is not None
        self.assertEqual(loaded_binprovider.name, "env")
        self.assertEqual(result.loaded_version, SemVer("1.8.0"))

    def test_update_uses_matching_provider_and_returns_loaded_binary(self):
        provider = EnvProvider()
        updated_bin = provider.load("python")
        assert updated_bin is not None

        binary = Binary(name="python", binproviders=[provider])
        with mock.patch.object(
            EnvProvider,
            "update",
            return_value=updated_bin,
            create=True,
        ) as mock_update:
            result = binary.update(binproviders=[provider.name])

        mock_update.assert_called_once_with("python")
        self.assertEqual(result.loaded_binprovider, provider)
        self.assertEqual(result.loaded_abspath, updated_bin.loaded_abspath)
        self.assertEqual(result.loaded_version, updated_bin.loaded_version)
        self.assertEqual(result.loaded_sha256, updated_bin.loaded_sha256)

    def test_uninstall_clears_loaded_fields(self):
        provider = EnvProvider()
        binary = Binary.model_validate(
            {
                "name": "python",
                "binproviders": [provider],
                "binprovider": provider,
                "abspath": sys.executable,
                "version": "1.2.3",
                "sha256": "unknown",
            },
        )

        with mock.patch.object(
            EnvProvider,
            "uninstall",
            return_value=True,
            create=True,
        ) as mock_uninstall:
            result = binary.uninstall(binproviders=[provider.name])

        mock_uninstall.assert_called_once_with("python")
        self.assertIsNone(result.loaded_binprovider)
        self.assertIsNone(result.loaded_abspath)
        self.assertIsNone(result.loaded_version)
        self.assertIsNone(result.loaded_sha256)
        self.assertEqual(result.binproviders_supported, [provider])
        self.assertFalse(result.is_valid)

    def test_uninstall_handles_stale_loaded_abspath(self):
        class NoopProvider(BinProvider):
            name: str = "noop"

        provider = NoopProvider()

        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            bin_path = bin_dir / "tool"
            bin_path.write_text("#!/bin/sh\n")
            bin_path.chmod(0o755)

            binary = Binary.model_validate(
                {
                    "name": "tool",
                    "binproviders": [provider],
                    "binprovider": provider,
                    "abspath": bin_path,
                    "version": "1.2.3",
                    "sha256": "unknown",
                },
            )

            bin_path.unlink()
            bin_dir.rmdir()

            with mock.patch.object(
                NoopProvider,
                "uninstall",
                return_value=True,
                create=True,
            ) as mock_uninstall:
                result = binary.uninstall(binproviders=[provider.name])

        mock_uninstall.assert_called_once_with("tool")
        self.assertIsNone(result.loaded_binprovider)
        self.assertIsNone(result.loaded_abspath)
        self.assertIsNone(result.loaded_version)
        self.assertIsNone(result.loaded_sha256)

    def _assert_binary_override_lifecycle(
        self,
        binary: Binary,
        provider: BinProvider,
        expected_install_args: tuple[str, ...],
        expected_commands: list[list[str]],
        extra_patches=(),
    ):
        provider_cls = type(provider)
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    provider_cls,
                    "INSTALLER_BIN_ABSPATH",
                    new_callable=mock.PropertyMock,
                    return_value=Path("/usr/local/bin/provider"),
                ),
            )
            mock_exec = stack.enter_context(
                mock.patch.object(provider_cls, "exec", return_value=proc),
            )
            stack.enter_context(
                mock.patch.object(
                    provider_cls,
                    "get_abspath",
                    return_value=Path(sys.executable),
                ),
            )
            stack.enter_context(
                mock.patch.object(
                    provider_cls,
                    "get_version",
                    return_value=SemVer("3.11.0"),
                ),
            )
            stack.enter_context(
                mock.patch.object(provider_cls, "get_sha256", return_value="unknown"),
            )
            for patcher in extra_patches:
                stack.enter_context(patcher)

            provider_with_overrides = binary.get_binprovider(provider.name)
            self.assertEqual(
                provider_with_overrides.get_install_args(binary.name),
                expected_install_args,
            )

            installed = binary.install()
            self.assertTrue(installed.is_valid)

            updated = binary.update()
            self.assertTrue(updated.is_valid)

            removed = updated.uninstall()
            self.assertFalse(removed.is_valid)

        self.assertEqual(
            [call.kwargs["cmd"] for call in mock_exec.call_args_list],
            expected_commands,
        )

    @mock.patch(
        "abx_pkg.binprovider_cargo.CargoProvider.load_PATH_from_cargo_root",
        lambda self: self,
    )
    def test_binary_cargo_override_install_args_used_for_install_update_uninstall(self):
        provider = CargoProvider(
            cargo_root=Path("/tmp/cargo-root"),
            cargo_home=Path("/tmp/cargo-home"),
            euid=os.geteuid(),
        )
        binary = Binary(
            name="rg",
            binproviders=[provider],
            overrides={"cargo": {"install_args": ["ripgrep"]}},
        )

        self._assert_binary_override_lifecycle(
            binary=binary,
            provider=provider,
            expected_install_args=("ripgrep",),
            expected_commands=[
                ["install", "--locked", "--root", "/tmp/cargo-root", "ripgrep"],
                [
                    "install",
                    "--force",
                    "--locked",
                    "--root",
                    "/tmp/cargo-root",
                    "ripgrep",
                ],
                ["uninstall", "--locked", "--root", "/tmp/cargo-root", "ripgrep"],
            ],
        )

    @mock.patch(
        "abx_pkg.binprovider_gem.GemProvider.load_PATH_from_gem_home",
        lambda self: self,
    )
    def test_binary_gem_override_install_args_used_for_install_update_uninstall(self):
        provider = GemProvider(
            gem_home=Path("/tmp/gem-home"),
            gem_bindir=Path("/tmp/gem-home/bin"),
            euid=os.geteuid(),
        )
        binary = Binary(
            name="rails-bin",
            binproviders=[provider],
            overrides={"gem": {"install_args": ["rake"]}},
        )

        self._assert_binary_override_lifecycle(
            binary=binary,
            provider=provider,
            expected_install_args=("rake",),
            expected_commands=[
                [
                    "install",
                    "--install-dir",
                    "/tmp/gem-home",
                    "--bindir",
                    "/tmp/gem-home/bin",
                    "--no-document",
                    "rake",
                ],
                [
                    "update",
                    "--install-dir",
                    "/tmp/gem-home",
                    "--bindir",
                    "/tmp/gem-home/bin",
                    "--no-document",
                    "rake",
                ],
                [
                    "uninstall",
                    "--all",
                    "--executables",
                    "--ignore-dependencies",
                    "--force",
                    "-i",
                    "/tmp/gem-home",
                    "rake",
                ],
            ],
        )

    @mock.patch(
        "abx_pkg.binprovider_go_get.GoGetProvider.load_PATH_from_go_env",
        lambda self: self,
    )
    def test_binary_go_get_override_install_args_used_for_install_and_update(self):
        provider = GoGetProvider(
            gobin=Path("/tmp/go/bin"),
            gopath=Path("/tmp/go"),
            euid=os.geteuid(),
        )
        binary = Binary(
            name="shfmt",
            binproviders=[provider],
            overrides={
                "go_get": {"install_args": ["mvdan.cc/sh/v3/cmd/shfmt@v3.11.0"]},
            },
        )

        self._assert_binary_override_lifecycle(
            binary=binary,
            provider=provider,
            expected_install_args=("mvdan.cc/sh/v3/cmd/shfmt@v3.11.0",),
            expected_commands=[
                ["install", "mvdan.cc/sh/v3/cmd/shfmt@v3.11.0"],
                ["install", "mvdan.cc/sh/v3/cmd/shfmt@v3.11.0"],
            ],
            extra_patches=(
                mock.patch.object(GoGetProvider, "uninstall", return_value=True),
            ),
        )

    @mock.patch(
        "abx_pkg.binprovider_nix.NixProvider.load_PATH_from_nix_profile",
        lambda self: self,
    )
    def test_binary_nix_override_install_args_used_for_install_update_uninstall(self):
        provider = NixProvider(
            nix_profile=Path("/tmp/nix/profile"),
            nix_state_dir=Path("/tmp/nix/state"),
            euid=os.geteuid(),
        )
        binary = Binary(
            name="jq-bin",
            binproviders=[provider],
            overrides={"nix": {"install_args": ["nixpkgs#jq"]}},
        )

        self._assert_binary_override_lifecycle(
            binary=binary,
            provider=provider,
            expected_install_args=("nixpkgs#jq",),
            expected_commands=[
                [
                    "profile",
                    "install",
                    "--extra-experimental-features",
                    "nix-command",
                    "--extra-experimental-features",
                    "flakes",
                    "--profile",
                    "/tmp/nix/profile",
                    "nixpkgs#jq",
                ],
                [
                    "profile",
                    "upgrade",
                    "--extra-experimental-features",
                    "nix-command",
                    "--extra-experimental-features",
                    "flakes",
                    "--profile",
                    "/tmp/nix/profile",
                    "jq",
                ],
                [
                    "profile",
                    "remove",
                    "--extra-experimental-features",
                    "nix-command",
                    "--extra-experimental-features",
                    "flakes",
                    "--profile",
                    "/tmp/nix/profile",
                    "jq",
                ],
            ],
            extra_patches=(
                mock.patch.object(Path, "mkdir"),
                mock.patch.object(Path, "exists", return_value=False),
                mock.patch.object(Path, "is_symlink", return_value=False),
                mock.patch.object(Path, "unlink"),
            ),
        )

    @mock.patch(
        "abx_pkg.binprovider_docker.DockerProvider.load_PATH_from_docker_shims",
        lambda self: self,
    )
    def test_binary_docker_override_install_args_used_for_install_update_uninstall(
        self,
    ):
        provider = DockerProvider(
            docker_shim_dir=Path("/tmp/docker-bin"),
            euid=os.geteuid(),
        )
        binary = Binary(
            name="shellcheck",
            binproviders=[provider],
            overrides={"docker": {"install_args": ["koalaman/shellcheck:v0.10.0"]}},
        )

        self._assert_binary_override_lifecycle(
            binary=binary,
            provider=provider,
            expected_install_args=("koalaman/shellcheck:v0.10.0",),
            expected_commands=[
                ["pull", "koalaman/shellcheck:v0.10.0"],
                ["pull", "koalaman/shellcheck:v0.10.0"],
                ["image", "rm", "--force", "koalaman/shellcheck:v0.10.0"],
            ],
            extra_patches=(
                mock.patch.object(DockerProvider, "_write_shim"),
                mock.patch.object(DockerProvider, "_write_metadata"),
                mock.patch.object(
                    DockerProvider,
                    "metadata_path",
                    return_value=Path("/tmp/docker-meta.json"),
                ),
            ),
        )


class TestUpdateAndUninstall(unittest.TestCase):
    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/brew"),
    )
    @mock.patch("abx_pkg.binprovider_brew.BrewProvider.load_PATH", lambda self: self)
    @mock.patch("abx_pkg.binprovider_pyinfra.PYINFRA_INSTALLED", False)
    @mock.patch("abx_pkg.binprovider_ansible.ANSIBLE_INSTALLED", False)
    def test_brew_provider_update_uses_upgrade_command(
        self,
        _mock_installer_bin_abspath,
    ):
        provider = BrewProvider()
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch("abx_pkg.binprovider_brew.time.time", return_value=0),
            mock.patch.object(
                BrewProvider,
                "exec",
                side_effect=[proc, proc],
            ) as mock_exec,
            mock.patch.object(
                BrewProvider,
                "get_abspath",
                return_value=Path(sys.executable),
            ),
            mock.patch.object(
                BrewProvider,
                "get_version",
                return_value=SemVer("3.11.0"),
            ),
            mock.patch.object(BrewProvider, "get_sha256", return_value="unknown"),
        ):
            provider.update("python")

        self.assertEqual(mock_exec.call_args_list[0].kwargs["cmd"], ["update"])
        self.assertEqual(
            mock_exec.call_args_list[1].kwargs["cmd"],
            ["upgrade", "python"],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/brew"),
    )
    @mock.patch("abx_pkg.binprovider_brew.BrewProvider.load_PATH", lambda self: self)
    @mock.patch("abx_pkg.binprovider_pyinfra.PYINFRA_INSTALLED", False)
    @mock.patch("abx_pkg.binprovider_ansible.ANSIBLE_INSTALLED", False)
    def test_brew_provider_uninstall_uses_uninstall_command(
        self,
        _mock_installer_bin_abspath,
    ):
        provider = BrewProvider()
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(BrewProvider, "exec", return_value=proc) as mock_exec:
            result = provider.uninstall("python")

        self.assertTrue(result)
        self.assertEqual(mock_exec.call_args.kwargs["cmd"], ["uninstall", "python"])

    @mock.patch("abx_pkg.binprovider_npm.NpmProvider._load_PATH", return_value="")
    def test_npm_provider_update_uses_update_command(
        self,
        _mock_load_path,
    ):
        provider = NpmProvider(npm_prefix=Path("/tmp/npm"), euid=os.geteuid())
        provider._INSTALLER_BIN_ABSPATH = Path("/usr/local/bin/pnpm")
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(NpmProvider, "exec", return_value=proc) as mock_exec,
            mock.patch.object(
                NpmProvider,
                "get_abspath",
                return_value=Path(sys.executable),
            ),
            mock.patch.object(
                NpmProvider,
                "get_version",
                return_value=SemVer("3.11.0"),
            ),
            mock.patch.object(NpmProvider, "get_sha256", return_value="unknown"),
        ):
            provider.update("python")

        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            [
                "update",
                "--loglevel=error",
                f"--store-dir={provider.cache_dir}",
                "--dir=/tmp/npm",
                "python",
            ],
        )

    @mock.patch("abx_pkg.binprovider_npm.NpmProvider._load_PATH", return_value="")
    def test_npm_provider_uninstall_uses_uninstall_command(
        self,
        _mock_load_path,
    ):
        provider = NpmProvider(npm_prefix=Path("/tmp/npm"), euid=os.geteuid())
        provider._INSTALLER_BIN_ABSPATH = Path("/usr/local/bin/pnpm")
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(NpmProvider, "exec", return_value=proc) as mock_exec:
            result = provider.uninstall("python")

        self.assertTrue(result)
        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            [
                "remove",
                "--loglevel=error",
                f"--store-dir={provider.cache_dir}",
                "--dir=/tmp/npm",
                "python",
            ],
        )

    @mock.patch("abx_pkg.binprovider_npm.NpmProvider._load_PATH", return_value="")
    def test_npm_provider_install_uses_npm_directly(self, _mock_load_path):
        provider = NpmProvider(npm_prefix=Path("/tmp/npm"), euid=os.geteuid())
        provider._INSTALLER_BIN_ABSPATH = Path("/usr/local/bin/npm")
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(NpmProvider, "exec", return_value=proc) as mock_exec:
            provider.default_install_handler("python", install_args=["python"])

        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            [
                "install",
                "--force",
                "--no-audit",
                "--no-fund",
                "--loglevel=error",
                provider.cache_arg,
                "--prefix=/tmp/npm",
                "python",
            ],
        )

    @mock.patch("abx_pkg.binprovider_npm.NpmProvider._load_PATH", return_value="")
    def test_npm_provider_prefers_npm_over_pnpm(self, _mock_load_path):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pnpm_bin = temp_path / "pnpm"
            npm_bin = temp_path / "npm"
            pnpm_bin.write_text("#!/bin/sh\n")
            npm_bin.write_text("#!/bin/sh\n")
            pnpm_bin.chmod(0o755)
            npm_bin.chmod(0o755)

            with mock.patch(
                "abx_pkg.binprovider_npm.bin_abspath",
                side_effect=lambda name, PATH=None: (
                    str(pnpm_bin)
                    if name == "pnpm"
                    else str(npm_bin)
                    if name == "npm"
                    else None
                ),
            ):
                provider = NpmProvider(euid=os.geteuid())
                installer_abspath = provider.INSTALLER_BIN_ABSPATH
                assert installer_abspath is not None
                self.assertEqual(
                    Path(installer_abspath).resolve(),
                    npm_bin.resolve(),
                )

    @mock.patch("abx_pkg.binprovider_npm.NpmProvider._load_PATH", return_value="")
    def test_npm_provider_respects_absolute_env_override_over_pnpm(
        self,
        _mock_load_path,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pnpm_bin = temp_path / "pnpm"
            npm_bin = temp_path / "npm"
            custom_bin = temp_path / "custom-npm"
            for bin_path in (pnpm_bin, npm_bin, custom_bin):
                bin_path.write_text("#!/bin/sh\n")
                bin_path.chmod(0o755)

            with (
                mock.patch.dict(os.environ, {"NPM_BINARY": str(custom_bin)}),
                mock.patch(
                    "abx_pkg.binprovider_npm.bin_abspath",
                    side_effect=lambda name, PATH=None: (
                        str(pnpm_bin)
                        if name == "pnpm"
                        else str(npm_bin)
                        if name == "npm"
                        else None
                    ),
                ),
            ):
                provider = NpmProvider(euid=os.geteuid())
                self.assertEqual(provider.INSTALLER_BIN_ABSPATH, custom_bin.resolve())

    @mock.patch.object(
        PipProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path(sys.executable),
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.PipProvider.load_PATH_from_pip_sitepackages",
        lambda self: self,
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.shutil.which",
        side_effect=lambda name, **kwargs: (
            "/usr/local/bin/uv"
            if name == "uv"
            else sys.executable
            if name == "pip"
            else None
        ),
    )
    def test_pip_provider_setup_bootstraps_uv(
        self,
        _mock_which,
        _mock_installer_bin_abspath,
    ):
        provider = PipProvider(euid=os.geteuid())
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            pip_venv = Path(temp_dir) / "venv"
            provider.pip_venv = pip_venv

            def fake_venv_create(path, **kwargs):
                python_path = Path(path) / "bin" / "python"
                python_path.parent.mkdir(parents=True, exist_ok=True)
                python_path.touch()
                python_path.chmod(0o755)
                pip_path = Path(path) / "bin" / "pip"
                pip_path.touch()
                pip_path.chmod(0o755)

            with (
                mock.patch("venv.create", side_effect=fake_venv_create) as mock_create,
                mock.patch.object(PipProvider, "exec", return_value=proc) as mock_exec,
            ):
                provider._pip_setup_venv(pip_venv)

        mock_create.assert_called_once_with(
            str(pip_venv),
            system_site_packages=False,
            clear=True,
            symlinks=True,
            with_pip=True,
            upgrade_deps=True,
        )
        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            [
                "pip",
                "install",
                "--quiet",
                "--python",
                str(pip_venv / "bin" / "python"),
                provider.cache_arg,
                "--upgrade",
                "pip",
                "setuptools",
                "uv",
            ],
        )

    @mock.patch.object(
        PipProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path(sys.executable),
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.PipProvider.load_PATH_from_pip_sitepackages",
        lambda self: self,
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.shutil.which",
        side_effect=lambda name, **kwargs: (
            "/usr/local/bin/uv"
            if name == "uv"
            else sys.executable
            if name == "pip"
            else None
        ),
    )
    def test_pip_provider_update_uses_install_upgrade(
        self,
        _mock_which,
        _mock_installer_bin_abspath,
    ):
        provider = PipProvider(euid=os.geteuid())
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch("abx_pkg.binprovider_pip.ACTIVE_VENV", None),
            mock.patch.object(PipProvider, "exec", return_value=proc) as mock_exec,
            mock.patch.object(
                PipProvider,
                "_uv_pip_target_args",
                return_value=["--python", "/tmp/python"],
            ),
            mock.patch.object(
                PipProvider,
                "get_abspath",
                return_value=Path(sys.executable),
            ),
            mock.patch.object(
                PipProvider,
                "get_version",
                return_value=SemVer("3.11.0"),
            ),
            mock.patch.object(PipProvider, "get_sha256", return_value="unknown"),
        ):
            provider.update("python")

        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            [
                "pip",
                "install",
                "--quiet",
                "--python",
                "/tmp/python",
                provider.cache_arg,
                "--upgrade",
                "python",
            ],
        )

    @mock.patch.object(
        PipProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path(sys.executable),
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.PipProvider.load_PATH_from_pip_sitepackages",
        lambda self: self,
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.shutil.which",
        side_effect=lambda name, **kwargs: (
            "/usr/local/bin/uv"
            if name == "uv"
            else "/usr/local/bin/pip"
            if name == "pip"
            else None
        ),
    )
    def test_pip_provider_uninstall_uses_uninstall_command(
        self,
        _mock_which,
        _mock_installer_bin_abspath,
    ):
        provider = PipProvider(euid=os.geteuid())
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(PipProvider, "exec", return_value=proc) as mock_exec,
            mock.patch.object(
                PipProvider,
                "_uv_pip_target_args",
                return_value=["--python", "/tmp/python"],
            ),
        ):
            result = provider.uninstall("python")

        self.assertTrue(result)
        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            ["pip", "uninstall", "--python", "/tmp/python", "python"],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path(sys.executable),
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.PipProvider.load_PATH_from_pip_sitepackages",
        lambda self: self,
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.shutil.which",
        side_effect=lambda name, **kwargs: (
            "/usr/local/bin/uv"
            if name == "uv"
            else sys.executable
            if name == "pip"
            else None
        ),
    )
    def test_pip_provider_show_uses_uv_pip_show(
        self,
        _mock_which,
        _mock_installer_bin_abspath,
    ):
        provider = PipProvider(euid=os.geteuid())
        proc = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Name: python\nVersion: 3.11.0\n",
            stderr="",
        )

        with (
            mock.patch.object(PipProvider, "exec", return_value=proc) as mock_exec,
            mock.patch.object(
                PipProvider,
                "_uv_pip_target_args",
                return_value=["--python", "/tmp/python"],
            ),
        ):
            provider._pip(["show", "--no-input", "python"], quiet=True)

        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            ["pip", "show", "--python", "/tmp/python", "python"],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path(sys.executable),
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.PipProvider.load_PATH_from_pip_sitepackages",
        lambda self: self,
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.shutil.which",
        side_effect=lambda name, **kwargs: (
            "/usr/local/bin/uv"
            if name == "uv"
            else sys.executable
            if name == "pip"
            else None
        ),
    )
    def test_pip_provider_version_falls_back_to_pip_show_when_binary_has_no_version_flag(
        self,
        _mock_which,
        _mock_installer_bin_abspath,
    ):
        provider = PipProvider(euid=os.geteuid())
        proc = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Name: opendataloader-pdf\nVersion: 2.0.2\n",
            stderr="",
        )

        with (
            mock.patch("abx_pkg.binprovider_pip.ACTIVE_VENV", None),
            mock.patch.object(
                BinProvider,
                "default_version_handler",
                side_effect=ValueError("no --version support"),
            ),
            mock.patch.object(PipProvider, "exec", return_value=proc),
            mock.patch.object(
                PipProvider,
                "_uv_pip_target_args",
                return_value=["--python", "/tmp/python"],
            ),
        ):
            version = provider.default_version_handler(
                "opendataloader-pdf",
                abspath=Path("/tmp/opendataloader-pdf"),
            )

        self.assertEqual(version, SemVer("2.0.2"))

    @mock.patch(
        "abx_pkg.binprovider_pip.PipProvider.load_PATH_from_pip_sitepackages",
        lambda self: self,
    )
    @mock.patch(
        "abx_pkg.binprovider_pip.shutil.which",
        side_effect=lambda name, **kwargs: (
            "/usr/local/bin/uv" if name == "uv" else None
        ),
    )
    def test_pip_provider_respects_explicit_pip_binary_abspath(
        self,
        _mock_which,
    ):
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            custom_pip = Path(temp_dir) / "pip"
            custom_pip.touch()
            custom_pip.chmod(0o755)

            with (
                mock.patch.dict(
                    os.environ,
                    {"PIP_BINARY": str(custom_pip)},
                    clear=False,
                ),
                mock.patch.object(PipProvider, "exec", return_value=proc) as mock_exec,
            ):
                provider = PipProvider(euid=os.geteuid())
                provider._pip(["install", "--no-input", "python"])

        self.assertEqual(mock_exec.call_args.kwargs["bin_name"], custom_pip)
        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            ["install", "--no-input", "python"],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/cargo"),
    )
    @mock.patch(
        "abx_pkg.binprovider_cargo.CargoProvider.load_PATH_from_cargo_root",
        lambda self: self,
    )
    def test_cargo_provider_update_uses_install_force(
        self,
        _mock_installer_bin_abspath,
    ):
        provider = CargoProvider(cargo_root=Path("/tmp/cargo-root"), euid=os.geteuid())
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(CargoProvider, "exec", return_value=proc) as mock_exec,
            mock.patch.object(
                CargoProvider,
                "get_abspath",
                return_value=Path(sys.executable),
            ),
            mock.patch.object(
                CargoProvider,
                "get_version",
                return_value=SemVer("3.11.0"),
            ),
            mock.patch.object(CargoProvider, "get_sha256", return_value="unknown"),
        ):
            provider.update("just")

        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            ["install", "--force", "--locked", "--root", "/tmp/cargo-root", "just"],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/cargo"),
    )
    @mock.patch(
        "abx_pkg.binprovider_cargo.CargoProvider.load_PATH_from_cargo_root",
        lambda self: self,
    )
    def test_cargo_provider_uninstall_uses_cargo_uninstall(
        self,
        _mock_installer_bin_abspath,
    ):
        provider = CargoProvider(cargo_root=Path("/tmp/cargo-root"), euid=os.geteuid())
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(CargoProvider, "exec", return_value=proc) as mock_exec:
            result = provider.uninstall("just")

        self.assertTrue(result)
        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            ["uninstall", "--locked", "--root", "/tmp/cargo-root", "just"],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/gem"),
    )
    @mock.patch(
        "abx_pkg.binprovider_gem.GemProvider.load_PATH_from_gem_home",
        lambda self: self,
    )
    def test_gem_provider_update_uses_gem_update(self, _mock_installer_bin_abspath):
        provider = GemProvider(
            gem_home=Path("/tmp/gem-home"),
            gem_bindir=Path("/tmp/gem-home/bin"),
            euid=os.geteuid(),
        )
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(GemProvider, "exec", return_value=proc) as mock_exec,
            mock.patch.object(
                GemProvider,
                "get_abspath",
                return_value=Path(sys.executable),
            ),
            mock.patch.object(
                GemProvider,
                "get_version",
                return_value=SemVer("3.11.0"),
            ),
            mock.patch.object(GemProvider, "get_sha256", return_value="unknown"),
        ):
            provider.update("lolcat")

        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            [
                "update",
                "--install-dir",
                "/tmp/gem-home",
                "--bindir",
                "/tmp/gem-home/bin",
                "--no-document",
                "lolcat",
            ],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/gem"),
    )
    @mock.patch(
        "abx_pkg.binprovider_gem.GemProvider.load_PATH_from_gem_home",
        lambda self: self,
    )
    def test_gem_provider_uninstall_uses_gem_uninstall(
        self,
        _mock_installer_bin_abspath,
    ):
        provider = GemProvider(
            gem_home=Path("/tmp/gem-home"),
            gem_bindir=Path("/tmp/gem-home/bin"),
            euid=os.geteuid(),
        )
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(GemProvider, "exec", return_value=proc) as mock_exec:
            result = provider.uninstall("lolcat")

        self.assertTrue(result)
        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            [
                "uninstall",
                "--all",
                "--executables",
                "--ignore-dependencies",
                "--force",
                "-i",
                "/tmp/gem-home",
                "lolcat",
            ],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/go"),
    )
    @mock.patch(
        "abx_pkg.binprovider_go_get.GoGetProvider.load_PATH_from_go_env",
        lambda self: self,
    )
    def test_go_get_provider_update_uses_go_install(self, _mock_installer_bin_abspath):
        provider = GoGetProvider(
            gobin=Path("/tmp/go/bin"),
            gopath=Path("/tmp/go"),
            euid=os.geteuid(),
        )
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(GoGetProvider, "exec", return_value=proc) as mock_exec,
            mock.patch.object(
                GoGetProvider,
                "get_abspath",
                return_value=Path(sys.executable),
            ),
            mock.patch.object(
                GoGetProvider,
                "get_version",
                return_value=SemVer("3.11.0"),
            ),
            mock.patch.object(GoGetProvider, "get_sha256", return_value="unknown"),
        ):
            provider.update("shfmt")

        self.assertEqual(mock_exec.call_args.kwargs["cmd"], ["install", "shfmt@latest"])

    @mock.patch(
        "abx_pkg.binprovider_go_get.GoGetProvider.load_PATH_from_go_env",
        lambda self: self,
    )
    def test_go_get_provider_uninstall_removes_binary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            gobin = Path(temp_dir) / "bin"
            gobin.mkdir(parents=True)
            bin_path = gobin / "shfmt"
            bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
            provider = GoGetProvider(
                gobin=gobin,
                gopath=Path(temp_dir),
                euid=os.geteuid(),
            )

            result = provider.uninstall("shfmt")

        self.assertTrue(result)
        self.assertFalse(bin_path.exists())

    @mock.patch.object(
        NixProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/nix"),
    )
    @mock.patch(
        "abx_pkg.binprovider_nix.NixProvider.load_PATH_from_nix_profile",
        lambda self: self,
    )
    def test_nix_provider_update_uses_profile_upgrade(
        self,
        _mock_installer_bin_abspath,
    ):
        provider = NixProvider(
            nix_profile=Path("/tmp/nix/profile"),
            nix_state_dir=Path("/tmp/nix/state"),
            euid=os.geteuid(),
        )
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(NixProvider, "exec", return_value=proc) as mock_exec,
            mock.patch.object(
                NixProvider,
                "get_abspath",
                return_value=Path(sys.executable),
            ),
            mock.patch.object(
                NixProvider,
                "get_version",
                return_value=SemVer("3.11.0"),
            ),
            mock.patch.object(NixProvider, "get_sha256", return_value="unknown"),
        ):
            provider.update("hello")

        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            [
                "profile",
                "upgrade",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                "/tmp/nix/profile",
                "hello",
            ],
        )

    @mock.patch.object(
        NixProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/nix"),
    )
    @mock.patch(
        "abx_pkg.binprovider_nix.NixProvider.load_PATH_from_nix_profile",
        lambda self: self,
    )
    def test_nix_provider_uninstall_uses_profile_remove(
        self,
        _mock_installer_bin_abspath,
    ):
        provider = NixProvider(
            nix_profile=Path("/tmp/nix/profile"),
            nix_state_dir=Path("/tmp/nix/state"),
            euid=os.geteuid(),
        )
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(NixProvider, "exec", return_value=proc) as mock_exec:
            result = provider.uninstall("hello")

        self.assertTrue(result)
        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            [
                "profile",
                "remove",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                "/tmp/nix/profile",
                "hello",
            ],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/docker"),
    )
    @mock.patch(
        "abx_pkg.binprovider_docker.DockerProvider.load_PATH_from_docker_shims",
        lambda self: self,
    )
    def test_docker_provider_update_uses_docker_pull(self, _mock_installer_bin_abspath):
        provider = DockerProvider(
            docker_shim_dir=Path("/tmp/docker-bin"),
            euid=os.geteuid(),
        )
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(DockerProvider, "exec", return_value=proc) as mock_exec,
            mock.patch.object(
                DockerProvider,
                "get_abspath",
                return_value=Path(sys.executable),
            ),
            mock.patch.object(
                DockerProvider,
                "get_version",
                return_value=SemVer("0.10.0"),
            ),
            mock.patch.object(DockerProvider, "get_sha256", return_value="unknown"),
            mock.patch.object(DockerProvider, "_write_shim"),
        ):
            provider.update("shellcheck")

        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            ["pull", "shellcheck:latest"],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/docker"),
    )
    @mock.patch(
        "abx_pkg.binprovider_docker.DockerProvider.load_PATH_from_docker_shims",
        lambda self: self,
    )
    def test_docker_provider_uninstall_uses_docker_image_rm(
        self,
        _mock_installer_bin_abspath,
    ):
        provider = DockerProvider(
            docker_shim_dir=Path("/tmp/docker-bin"),
            euid=os.geteuid(),
        )
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(DockerProvider, "exec", return_value=proc) as mock_exec,
            mock.patch.object(Path, "exists", return_value=False),
        ):
            result = provider.uninstall("shellcheck")

        self.assertTrue(result)
        self.assertEqual(
            mock_exec.call_args.kwargs["cmd"],
            ["image", "rm", "--force", "shellcheck:latest"],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/bin/apt-get"),
    )
    @mock.patch(
        "abx_pkg.binprovider_apt.shutil.which",
        side_effect=lambda name: (
            "/usr/bin/dpkg" if name == "dpkg" else "/usr/bin/apt-get"
        ),
    )
    @mock.patch("abx_pkg.binprovider_pyinfra.PYINFRA_INSTALLED", False)
    @mock.patch("abx_pkg.binprovider_ansible.ANSIBLE_INSTALLED", False)
    def test_apt_provider_update_uses_only_upgrade(
        self,
        _mock_installer_bin_abspath,
        _mock_which,
    ):
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            mock.patch("abx_pkg.binprovider_apt.time.time", return_value=0),
            mock.patch.object(
                AptProvider,
                "exec",
                side_effect=[proc, proc, proc],
            ) as mock_exec,
            mock.patch.object(
                AptProvider,
                "get_abspath",
                return_value=Path(sys.executable),
            ),
            mock.patch.object(
                AptProvider,
                "get_version",
                return_value=SemVer("3.11.0"),
            ),
            mock.patch.object(AptProvider, "get_sha256", return_value="unknown"),
        ):
            provider = AptProvider()
            provider.update("python")

        self.assertEqual(mock_exec.call_args_list[1].kwargs["cmd"], ["update", "-qq"])
        self.assertEqual(
            mock_exec.call_args_list[2].kwargs["cmd"],
            [
                "install",
                "--only-upgrade",
                "-y",
                "-qq",
                "--no-install-recommends",
                "python",
            ],
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/bin/apt-get"),
    )
    @mock.patch(
        "abx_pkg.binprovider_apt.shutil.which",
        side_effect=lambda name: (
            "/usr/bin/dpkg" if name == "dpkg" else "/usr/bin/apt-get"
        ),
    )
    @mock.patch("abx_pkg.binprovider_pyinfra.PYINFRA_INSTALLED", False)
    @mock.patch("abx_pkg.binprovider_ansible.ANSIBLE_INSTALLED", False)
    def test_apt_provider_uninstall_uses_remove(
        self,
        _mock_installer_bin_abspath,
        _mock_which,
    ):
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(
            AptProvider,
            "exec",
            side_effect=[proc, proc],
        ) as mock_exec:
            provider = AptProvider()
            result = provider.uninstall("python")

        self.assertTrue(result)
        self.assertEqual(
            mock_exec.call_args_list[1].kwargs["cmd"],
            ["remove", "-y", "-qq", "python"],
        )

    @mock.patch(
        "abx_pkg.binprovider_pyinfra.pyinfra_package_install",
        return_value="updated",
    )
    @mock.patch.object(
        PyinfraProvider,
        "get_abspath",
        return_value=Path(sys.executable),
    )
    @mock.patch.object(PyinfraProvider, "get_version", return_value=SemVer("3.11.0"))
    @mock.patch.object(PyinfraProvider, "get_sha256", return_value="unknown")
    def test_pyinfra_provider_update_uses_latest_state(
        self,
        _mock_sha256,
        _mock_version,
        _mock_abspath,
        mock_pyinfra_install,
    ):
        provider = PyinfraProvider(
            pyinfra_installer_module="operations.server.packages",
        )

        provider.update("python")

        mock_pyinfra_install.assert_called_once_with(
            pkg_names=("python",),
            installer_module="operations.server.packages",
            installer_extra_kwargs={"latest": True},
        )

    @mock.patch(
        "abx_pkg.binprovider_pyinfra.pyinfra_package_install",
        return_value="removed",
    )
    def test_pyinfra_provider_uninstall_uses_absent_state(self, mock_pyinfra_install):
        provider = PyinfraProvider(
            pyinfra_installer_module="operations.server.packages",
        )

        result = provider.uninstall("python")

        self.assertTrue(result)
        mock_pyinfra_install.assert_called_once_with(
            pkg_names=("python",),
            installer_module="operations.server.packages",
            installer_extra_kwargs={"present": False},
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/ansible"),
    )
    @mock.patch(
        "abx_pkg.binprovider_ansible.ansible_package_install",
        return_value="updated",
    )
    @mock.patch.object(
        AnsibleProvider,
        "get_abspath",
        return_value=Path(sys.executable),
    )
    @mock.patch.object(AnsibleProvider, "get_version", return_value=SemVer("3.11.0"))
    @mock.patch.object(AnsibleProvider, "get_sha256", return_value="unknown")
    def test_ansible_provider_update_uses_latest_state(
        self,
        _mock_sha256,
        _mock_version,
        _mock_abspath,
        mock_ansible_install,
        _mock_installer_bin_abspath,
    ):
        provider = AnsibleProvider(ansible_installer_module="ansible.builtin.package")

        provider.update("python")

        mock_ansible_install.assert_called_once_with(
            pkg_names=("python",),
            quiet=True,
            playbook_template=provider.ansible_playbook_template,
            installer_module="ansible.builtin.package",
            state="latest",
        )

    @mock.patch.object(
        BinProvider,
        "INSTALLER_BIN_ABSPATH",
        new_callable=mock.PropertyMock,
        return_value=Path("/usr/local/bin/ansible"),
    )
    @mock.patch(
        "abx_pkg.binprovider_ansible.ansible_package_install",
        return_value="removed",
    )
    def test_ansible_provider_uninstall_uses_absent_state(
        self,
        mock_ansible_install,
        _mock_installer_bin_abspath,
    ):
        provider = AnsibleProvider(ansible_installer_module="ansible.builtin.package")

        result = provider.uninstall("python")

        self.assertTrue(result)
        mock_ansible_install.assert_called_once_with(
            pkg_names=("python",),
            quiet=True,
            playbook_template=provider.ansible_playbook_template,
            installer_module="ansible.builtin.package",
            state="absent",
        )


def flatten(xss):
    return [x for xs in xss for x in xs]


def brew_formula_is_installed(package: str) -> bool:
    brew = shutil.which("brew")
    if not brew:
        return False
    return (
        subprocess.run(
            [brew, "list", "--formula", package],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def apt_package_is_installed(package: str) -> bool:
    dpkg = shutil.which("dpkg")
    if not dpkg:
        return False
    return (
        subprocess.run([dpkg, "-s", package], capture_output=True, text=True).returncode
        == 0
    )


def docker_daemon_is_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    return (
        subprocess.run([docker, "info"], capture_output=True, text=True).returncode == 0
    )


def gem_package_is_installed(package: str) -> bool:
    gem = shutil.which("gem")
    if not gem:
        return False
    return bool(
        subprocess.run(
            [gem, "list", f"^{package}$", "-a"],
            capture_output=True,
            text=True,
        ).stdout.strip(),
    )


class LiveUpdateAndUninstallTest(unittest.TestCase):
    def assert_binary_loaded_state(self, binary: Binary, version_args=("--version",)):
        self.assertTrue(binary.is_valid)
        self.assertIsNotNone(binary.loaded_binprovider)
        self.assertIsNotNone(binary.loaded_abspath)
        self.assertIsNotNone(binary.loaded_version)

        provider = binary.loaded_binprovider
        assert provider is not None
        self.assertEqual(
            provider.get_abspath(binary.name, quiet=True, nocache=True),
            binary.loaded_abspath,
        )
        self.assertEqual(
            provider.get_version(binary.name, quiet=True, nocache=True),
            binary.loaded_version,
        )
        self.assertEqual(binary.exec(cmd=version_args, quiet=True).returncode, 0)

    def assert_binary_unloaded_state(self, binary: Binary):
        self.assertFalse(binary.is_valid)
        self.assertIsNone(binary.loaded_binprovider)
        self.assertIsNone(binary.loaded_abspath)
        self.assertIsNone(binary.loaded_version)
        self.assertIsNone(binary.loaded_sha256)

    def assert_binary_missing(self, binary: Binary):
        provider = binary.binproviders_supported[0]
        self.assertIsNone(provider.load(binary.name, quiet=True, nocache=True))
        with self.assertRaises(Exception):
            binary.load(nocache=True)

    def run_lifecycle_phase(self, binary: Binary, phase: str, func, details: str = ""):
        provider_name = binary.binproviders_supported[0].name
        prefix = f"[live:{provider_name}:{binary.name}] {phase}"
        if details:
            prefix = f"{prefix} {details}"

        print(f"{prefix} START", file=sys.stderr, flush=True)
        started = time.perf_counter()
        try:
            result = func()
        except Exception as err:
            elapsed = time.perf_counter() - started
            print(f"{prefix} FAIL {elapsed:.2f}s {err}", file=sys.stderr, flush=True)
            raise

        elapsed = time.perf_counter() - started
        print(f"{prefix} OK {elapsed:.2f}s", file=sys.stderr, flush=True)
        return result

    def assert_binary_lifecycle(
        self,
        binary: Binary,
        version_args=("--version",),
        override_binary: Binary | None = None,
        override_version_args=("--version",),
    ):
        base_provider = binary.binproviders_supported[0]
        binaries_to_cleanup = [binary, *([override_binary] if override_binary else [])]

        try:
            self.run_lifecycle_phase(
                binary,
                "load-missing",
                lambda: self.assert_binary_missing(binary),
            )
            self.run_lifecycle_phase(
                binary,
                "get-install-args",
                lambda: self.assertTrue(
                    binary.get_binprovider(base_provider.name).get_install_args(
                        binary.name,
                    ),
                ),
            )

            loaded_or_installed = self.run_lifecycle_phase(
                binary,
                "load-or-install",
                lambda: binary.load_or_install(nocache=True),
                details=f"install_args={base_provider.get_install_args(binary.name)}",
            )
            self.run_lifecycle_phase(
                binary,
                "verify-load-or-install",
                lambda: self.assert_binary_loaded_state(
                    loaded_or_installed,
                    version_args=version_args,
                ),
            )

            loaded = self.run_lifecycle_phase(
                binary,
                "load",
                lambda: binary.load(nocache=True),
            )
            self.run_lifecycle_phase(
                binary,
                "verify-load",
                lambda: self.assert_binary_loaded_state(
                    loaded,
                    version_args=version_args,
                ),
            )

            updated = self.run_lifecycle_phase(
                binary,
                "update",
                lambda: loaded_or_installed.update(),
                details=f"install_args={base_provider.get_install_args(binary.name)}",
            )
            self.run_lifecycle_phase(
                binary,
                "verify-update",
                lambda: self.assert_binary_loaded_state(
                    updated,
                    version_args=version_args,
                ),
            )

            removed = self.run_lifecycle_phase(
                binary,
                "uninstall",
                lambda: updated.uninstall(),
                details=f"install_args={base_provider.get_install_args(binary.name)}",
            )
            self.run_lifecycle_phase(
                binary,
                "verify-uninstall",
                lambda: self.assert_binary_unloaded_state(removed),
            )
            self.run_lifecycle_phase(
                binary,
                "verify-missing-after-uninstall",
                lambda: self.assert_binary_missing(binary),
            )

            installed = self.run_lifecycle_phase(
                binary,
                "install",
                lambda: binary.install(),
                details=f"install_args={base_provider.get_install_args(binary.name)}",
            )
            self.run_lifecycle_phase(
                binary,
                "verify-install",
                lambda: self.assert_binary_loaded_state(
                    installed,
                    version_args=version_args,
                ),
            )
            removed_after_install = self.run_lifecycle_phase(
                binary,
                "uninstall-after-install",
                lambda: installed.uninstall(),
                details=f"install_args={base_provider.get_install_args(binary.name)}",
            )
            self.run_lifecycle_phase(
                binary,
                "verify-uninstall-after-install",
                lambda: self.assert_binary_unloaded_state(removed_after_install),
            )
            self.run_lifecycle_phase(
                binary,
                "verify-missing-after-install-cycle",
                lambda: self.assert_binary_missing(binary),
            )

            if override_binary:
                override_provider = override_binary.get_binprovider(base_provider.name)
                override_install_args = tuple(
                    override_provider.get_install_args(override_binary.name),
                )

                self.run_lifecycle_phase(
                    override_binary,
                    "load-missing-override",
                    lambda: self.assert_binary_missing(override_binary),
                )
                override_provider = override_binary.get_binprovider(base_provider.name)
                self.run_lifecycle_phase(
                    override_binary,
                    "get-install-args-override",
                    lambda: self.assertEqual(
                        override_provider.get_install_args(override_binary.name),
                        override_install_args,
                    ),
                )

                override_installed = self.run_lifecycle_phase(
                    override_binary,
                    "install-override",
                    lambda: override_binary.install(),
                    details=f"install_args={override_install_args}",
                )
                self.run_lifecycle_phase(
                    override_binary,
                    "verify-install-override",
                    lambda: self.assert_binary_loaded_state(
                        override_installed,
                        version_args=override_version_args,
                    ),
                )

                override_updated = self.run_lifecycle_phase(
                    override_binary,
                    "update-override",
                    lambda: override_installed.update(),
                    details=f"install_args={override_install_args}",
                )
                self.run_lifecycle_phase(
                    override_binary,
                    "verify-update-override",
                    lambda: self.assert_binary_loaded_state(
                        override_updated,
                        version_args=override_version_args,
                    ),
                )

                override_removed = self.run_lifecycle_phase(
                    override_binary,
                    "uninstall-override",
                    lambda: override_updated.uninstall(),
                    details=f"install_args={override_install_args}",
                )
                self.run_lifecycle_phase(
                    override_binary,
                    "verify-uninstall-override",
                    lambda: self.assert_binary_unloaded_state(override_removed),
                )
                self.run_lifecycle_phase(
                    override_binary,
                    "verify-missing-after-uninstall-override",
                    lambda: self.assert_binary_missing(override_binary),
                )
        finally:
            for candidate in binaries_to_cleanup:
                provider = candidate.get_binprovider(
                    candidate.binproviders_supported[0].name,
                )
                try:
                    provider.uninstall(candidate.name, quiet=True, nocache=True)
                except Exception as err:
                    print(
                        f"[live:{provider.name}:{candidate.name}] cleanup-ignore {err}",
                        file=sys.stderr,
                        flush=True,
                    )

    def make_override_binary(self, binary: Binary, install_args: list[str]) -> Binary:
        provider_name = binary.binproviders_supported[0].name
        return Binary(
            name=binary.name,
            binproviders=binary.binproviders_supported,
            overrides={
                **binary.overrides,
                provider_name: {
                    **binary.overrides.get(provider_name, {}),
                    "install_args": install_args,
                },
            },
        )

    def pick_missing_brew_formula(self) -> str:
        provider = BrewProvider()
        for formula in ("hello", "jq", "watch", "fzy"):
            if brew_formula_is_installed(formula):
                continue
            if provider.load(formula, quiet=True, nocache=True) is not None:
                continue
            return formula
        raise unittest.SkipTest(
            "No safe missing brew formula candidates were available for a live lifecycle test",
        )

    def pick_missing_apt_package(self) -> str:
        provider = AptProvider()
        for package in ("jq", "tree", "rename"):
            if apt_package_is_installed(package):
                continue
            if provider.load(package, quiet=True, nocache=True) is not None:
                continue
            return package
        raise unittest.SkipTest(
            "No safe missing apt package candidates were available for a live lifecycle test",
        )

    def pick_missing_gem_package(self) -> str:
        for package in ("lolcat", "cowsay"):
            if gem_package_is_installed(package):
                continue
            return package
        raise unittest.SkipTest(
            "No safe missing gem package candidates were available for a live lifecycle test",
        )

    def test_pip_provider_live_update_and_uninstall(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = PipProvider(pip_venv=Path(temp_dir) / "venv")
            binary = Binary(name="black", binproviders=[provider])
            self.assert_binary_lifecycle(binary)

    def test_pip_provider_live_venv_setup_respects_explicit_pip_binary_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pip_venv = temp_path / "venv"
            custom_pip = temp_path / "custom-pip"
            pip_used_flag = temp_path / "custom-pip-used"
            custom_pip.write_text(
                "#!/bin/sh\n"
                'touch "$CUSTOM_PIP_MARKER"\n'
                "echo 'custom pip was invoked for pip_venv setup' >&2\n"
                "exit 99\n",
            )
            custom_pip.chmod(0o755)

            old_pip_binary = os.environ.get("PIP_BINARY")
            old_pip_marker = os.environ.get("CUSTOM_PIP_MARKER")
            try:
                os.environ["PIP_BINARY"] = str(custom_pip)
                os.environ["CUSTOM_PIP_MARKER"] = str(pip_used_flag)

                provider = PipProvider(pip_venv=pip_venv)
                with self.assertRaises(Exception) as err:
                    provider.setup()
            finally:
                if old_pip_binary is None:
                    os.environ.pop("PIP_BINARY", None)
                else:
                    os.environ["PIP_BINARY"] = old_pip_binary
                if old_pip_marker is None:
                    os.environ.pop("CUSTOM_PIP_MARKER", None)
                else:
                    os.environ["CUSTOM_PIP_MARKER"] = old_pip_marker

            self.assertTrue(
                pip_used_flag.exists(),
                "pip_venv setup did not invoke explicit PIP_BINARY",
            )
            self.assertIn(
                "custom pip was invoked for pip_venv setup",
                str(err.exception),
            )
            self.assertFalse((pip_venv / "bin" / "uv").exists())

    def test_npm_provider_live_update_and_uninstall(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NpmProvider(npm_prefix=Path(temp_dir) / "npm")
            binary = Binary(name="esbuild", binproviders=[provider])
            self.assert_binary_lifecycle(binary)

    def test_cargo_provider_live_update_and_uninstall(self):
        if not shutil.which("cargo"):
            raise unittest.SkipTest("cargo is not available on this host")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = CargoProvider(
                cargo_root=Path(temp_dir) / "cargo",
                cargo_home=Path(temp_dir) / "cargo-home",
            )
            binary = Binary(name="choose", binproviders=[provider])
            override_binary = self.make_override_binary(binary, ["choose"])
            self.assert_binary_lifecycle(binary, override_binary=override_binary)

    def test_gem_provider_live_update_and_uninstall(self):
        if not shutil.which("gem"):
            raise unittest.SkipTest("gem is not available on this host")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GemProvider(
                gem_home=Path(temp_dir) / "gem-home",
                gem_bindir=Path(temp_dir) / "gem-home/bin",
            )
            gem_package = self.pick_missing_gem_package()
            binary = Binary(name=gem_package, binproviders=[provider])
            self.assert_binary_lifecycle(
                binary,
                override_binary=self.make_override_binary(binary, [gem_package]),
            )

    def test_go_get_provider_live_update_and_uninstall(self):
        if not shutil.which("go"):
            raise unittest.SkipTest("go is not available on this host")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = GoGetProvider(
                gobin=Path(temp_dir) / "go/bin",
                gopath=Path(temp_dir) / "go",
            )
            binary = Binary(
                name="shfmt",
                binproviders=[provider],
                overrides={
                    "go_get": {"install_args": ["mvdan.cc/sh/v3/cmd/shfmt@latest"]},
                },
            )
            self.assert_binary_lifecycle(binary)

    def test_nix_provider_live_update_and_uninstall(self):
        if not NixProvider().INSTALLER_BIN_ABSPATH:
            raise unittest.SkipTest("nix is not available on this host")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = NixProvider(
                nix_profile=Path(temp_dir) / "nix-profile",
                nix_state_dir=Path(temp_dir) / "nix-state",
            )
            binary = Binary(name="jq", binproviders=[provider])
            self.assert_binary_lifecycle(
                binary,
                override_binary=self.make_override_binary(binary, ["nixpkgs#jq"]),
            )

    def test_docker_provider_live_update_and_uninstall(self):
        if not docker_daemon_is_available():
            raise unittest.SkipTest("docker daemon is not available on this host")

        with tempfile.TemporaryDirectory() as temp_dir:
            provider = DockerProvider(docker_shim_dir=Path(temp_dir) / "docker/bin")
            binary = Binary(
                name="shellcheck",
                binproviders=[provider],
                overrides={"docker": {"install_args": ["koalaman/shellcheck:v0.10.0"]}},
            )
            self.assert_binary_lifecycle(binary)

    def test_brew_provider_live_update_and_uninstall(self):
        if not shutil.which("brew"):
            raise unittest.SkipTest("brew is not available on this host")

        provider = BrewProvider()
        binary = Binary(name=self.pick_missing_brew_formula(), binproviders=[provider])
        self.assert_binary_lifecycle(binary)

    def test_pyinfra_provider_live_update_and_uninstall(self):
        if not shutil.which("pyinfra"):
            raise unittest.SkipTest("pyinfra is not available on this host")

        if "linux" in sys.platform and shutil.which("apt-get"):
            if os.geteuid() != 0:
                raise unittest.SkipTest(
                    "pyinfra apt lifecycle tests require root on Linux",
                )
            provider = PyinfraProvider(
                pyinfra_installer_module="operations.apt.packages",
            )
            binary = Binary(
                name=self.pick_missing_apt_package(),
                binproviders=[provider],
            )
        elif shutil.which("brew"):
            provider = PyinfraProvider(
                pyinfra_installer_module="operations.brew.packages",
            )
            binary = Binary(
                name=self.pick_missing_brew_formula(),
                binproviders=[provider],
            )
        else:
            raise unittest.SkipTest("Neither apt nor brew is available on this host")

        self.assert_binary_lifecycle(binary)

    def test_ansible_provider_live_update_and_uninstall(self):
        if not shutil.which("ansible"):
            raise unittest.SkipTest("ansible is not available on this host")

        if "linux" in sys.platform and shutil.which("apt-get"):
            if os.geteuid() != 0:
                raise unittest.SkipTest(
                    "ansible apt lifecycle tests require root on Linux",
                )
            provider = AnsibleProvider(ansible_installer_module="ansible.builtin.apt")
            binary = Binary(
                name=self.pick_missing_apt_package(),
                binproviders=[provider],
            )
        elif shutil.which("brew"):
            provider = AnsibleProvider(
                ansible_installer_module="community.general.homebrew",
            )
            binary = Binary(
                name=self.pick_missing_brew_formula(),
                binproviders=[provider],
            )
        else:
            raise unittest.SkipTest("Neither apt nor brew is available on this host")

        self.assert_binary_lifecycle(binary)

    def test_apt_provider_live_update_and_uninstall(self):
        if "linux" not in sys.platform:
            raise unittest.SkipTest("apt live lifecycle tests only run on Linux hosts")
        if not shutil.which("apt-get"):
            raise unittest.SkipTest("apt-get is not available on this host")
        if os.geteuid() != 0:
            raise unittest.SkipTest("apt lifecycle tests require root on Linux")

        provider = AptProvider()
        binary = Binary(name=self.pick_missing_apt_package(), binproviders=[provider])
        self.assert_binary_lifecycle(binary)


class InstallTest(unittest.TestCase):
    def install_with_binprovider(self, provider, binary):

        binary_bin = binary.load_or_install()
        provider_bin = provider.load_or_install(bin_name=binary.name)
        # print(binary_bin, binary_bin.bin_dir, binary_bin.loaded_abspath)
        # print('\n'.join(f'{provider}={path}' for provider, path in binary.loaded_abspaths.items()), '\n')
        # print()
        try:
            self.assertEqual(
                binary_bin.loaded_binprovider,
                provider_bin.loaded_binprovider,
            )
        except AssertionError:
            print("binary_bin", dict(binary_bin.loaded_binprovider))
            print("provider_bin", dict(provider_bin.loaded_binprovider))
            raise
        self.assertEqual(binary_bin.loaded_abspath, provider_bin.loaded_abspath)
        self.assertEqual(binary_bin.loaded_version, provider_bin.loaded_version)
        self.assertEqual(binary_bin.loaded_sha256, provider_bin.loaded_sha256)

        self.assertIn(
            binary_bin.loaded_abspath,
            flatten(binary_bin.loaded_abspaths.values()),
        )
        self.assertIn(
            str(binary_bin.bin_dir),
            flatten(PATH.split(":") for PATH in binary_bin.loaded_bin_dirs.values()),
        )

        PATH = provider.PATH
        bin_abspath = shutil.which(binary.name, path=PATH)
        assert bin_abspath, f"Could not find {binary.name} in PATH={PATH}"
        VERSION = SemVer.parse(
            subprocess.check_output(f"{bin_abspath} --version", shell=True, text=True),
        )
        ABSPATH = Path(bin_abspath).absolute().resolve()

        self.assertEqual(binary_bin.loaded_version, VERSION)
        self.assertIn(binary_bin.loaded_abspath, provider.get_abspaths(binary_bin.name))
        self.assertEqual(binary_bin.loaded_respath, ABSPATH)
        self.assertTrue(binary_bin.is_valid)
        self.assertTrue(binary_bin.is_executable)
        self.assertFalse(binary_bin.is_script)
        self.assertTrue(
            bool(str(binary_bin)),
        )  # easy way to make sure serializing doesn't throw an error
        # print(provider.PATH)
        # print()
        # print()
        # print(binary.bin_filename, binary.bin_dir, binary.loaded_abspaths)
        # print()
        # print()
        # print(provider.name, 'PATH=', provider.PATH, 'ABSPATHS=', provider.get_abspaths(bin_name=binary_bin.name))
        return provider_bin

    def test_env_provider(self):
        provider = EnvProvider()
        binary = Binary(name="wget", binproviders=[provider]).load()
        self.install_with_binprovider(provider, binary)

    def test_pip_provider(self):
        # pipprovider = PipProvider()
        pip_venv = os.environ.get("VIRTUAL_ENV", None)
        pipprovider = PipProvider(pip_venv=Path(pip_venv) if pip_venv else None)
        # print('PIP BINPROVIDER', pipprovider.INSTALLER_BIN_ABSPATH, 'PATH=', pipprovider.PATH)
        binary = Binary(name="yt-dlp", binproviders=[pipprovider])
        self.install_with_binprovider(pipprovider, binary)

    def test_npm_provider(self):
        npmprovider = NpmProvider()
        # print(provider.PATH)
        binary = Binary(name="tsx", binproviders=[npmprovider])
        self.install_with_binprovider(npmprovider, binary)

    @mock.patch("sys.stderr")
    @mock.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        ),
    )
    def test_dry_run_doesnt_exec(self, mock_run, _mock_stderr):
        pipprovider = PipProvider().get_provider_with_overrides(dry_run=True)
        pipprovider.install(bin_name="doesnotexist")
        mock_run.assert_not_called()

    @mock.patch("sys.stderr")
    @mock.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        ),
    )
    def test_env_dry_run_doesnt_exec(self, mock_run, _mock_stderr):
        with mock.patch.dict(os.environ, {"DRY_RUN": "1"}, clear=False):
            PipProvider().install(bin_name="doesnotexist")
        mock_run.assert_not_called()

    def test_dry_run_logs_info(self):
        pipprovider = PipProvider()
        binary = Binary(name="doesnotexist", binproviders=[pipprovider])
        with capture_abx_logs(logging.INFO) as records:
            binary.install(dry_run=True)

        messages = [record.getMessage() for record in records]
        self.assertTrue(
            any(message.startswith("DRY RUN (PipProvider): ") for message in messages),
        )

    def test_env_dry_run_logs_info(self):
        pipprovider = PipProvider()
        binary = Binary(name="doesnotexist", binproviders=[pipprovider])
        with (
            mock.patch.dict(os.environ, {"DRY_RUN": "1"}, clear=False),
            capture_abx_logs(logging.INFO) as records,
        ):
            binary.install()

        messages = [record.getMessage() for record in records]
        self.assertTrue(
            any(message.startswith("DRY RUN (PipProvider): ") for message in messages),
        )

    def test_brew_provider(self):
        # print(provider.PATH)
        os.environ["HOMEBREW_NO_AUTO_UPDATE"] = "True"
        os.environ["HOMEBREW_NO_INSTALL_CLEANUP"] = "True"
        os.environ["HOMEBREW_NO_ENV_HINTS"] = "True"

        is_on_windows = sys.platform.lower().startswith("win") or os.name == "nt"
        is_on_macos = "darwin" in sys.platform.lower()
        is_on_linux = "linux" in sys.platform.lower()
        has_brew = shutil.which("brew")
        # has_apt = shutil.which('dpkg') is not None

        provider = BrewProvider()
        if has_brew:
            self.assertTrue(provider.PATH)
            self.assertTrue(provider.is_valid)
        else:
            # print('SHOULD NOT HAVE BREW, but got', provider.INSTALLER_BIN_ABSPATH, 'PATH=', provider.PATH)
            self.assertFalse(provider.is_valid)

        exception = None
        result = None
        try:
            binary = Binary(name="wget", binproviders=[provider])
            result = self.install_with_binprovider(provider, binary)
        except Exception as err:
            exception = err

        if is_on_macos or (is_on_linux and has_brew):
            self.assertTrue(has_brew)
            if exception:
                raise exception
            self.assertIsNone(exception)
            self.assertTrue(result)
        elif is_on_windows or (is_on_linux and not has_brew):
            self.assertFalse(has_brew)
            self.assertIsInstance(exception, Exception)
            self.assertFalse(result)
        else:
            assert exception is not None
            raise exception

    def test_apt_provider(self):
        is_on_windows = sys.platform.startswith("win") or os.name == "nt"
        is_on_macos = "darwin" in sys.platform
        is_on_linux = "linux" in sys.platform
        # has_brew = shutil.which('brew') is not None
        has_apt = shutil.which("apt-get") is not None

        exception = None
        result = None
        provider = AptProvider()
        if has_apt:
            self.assertTrue(provider.PATH)
        else:
            self.assertFalse(provider.PATH)
        try:
            # print(provider.PATH)
            binary = Binary(name="wget", binproviders=[provider])
            result = self.install_with_binprovider(provider, binary)
        except Exception as err:
            exception = err

        if is_on_linux:
            self.assertTrue(has_apt)
            if exception:
                raise exception
            self.assertIsNone(exception)
            self.assertTrue(result)
        elif is_on_windows or is_on_macos:
            self.assertFalse(has_apt)
            self.assertIsInstance(exception, Exception)
            self.assertFalse(result)
        else:
            assert exception is not None
            raise exception


if __name__ == "__main__":
    unittest.main()
