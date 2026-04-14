__package__ = "abxpkg"

import functools
import importlib.util
import inspect
import logging as py_logging
import os
import shlex
import subprocess
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from collections.abc import Callable

from typing_extensions import ParamSpec, TypeVar

from .semver import SemVer
from .exceptions import BinaryOperationError

LOGGER_NAME = "abxpkg"
DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
RICH_INSTALLED = importlib.util.find_spec("rich") is not None
TRACE_INDENT = "    "

if TYPE_CHECKING:
    from rich.console import Console

logger = py_logging.getLogger(LOGGER_NAME)
if not any(isinstance(handler, py_logging.NullHandler) for handler in logger.handlers):
    logger.addHandler(py_logging.NullHandler())
logger.setLevel(py_logging.WARNING)

P = ParamSpec("P")
R = TypeVar("R")
TRACE_DEPTH: ContextVar[int] = ContextVar("abxpkg_trace_depth", default=0)
TRACE_DEPTH_OVERRIDE: ContextVar[int | None] = ContextVar(
    "abxpkg_trace_depth_override",
    default=None,
)
ORIGINAL_LOG_RECORD_FACTORY = py_logging.getLogRecordFactory()
CURRENT_HOME = os.path.expanduser("~")
CURRENT_WORKING_DIR = os.getcwd()
DARK_GREY = "\x1b[90m"
GREEN = "\x1b[32m"
DIM_RED = "\x1b[2;31m"
ANSI_RESET = "\x1b[0m"
GREEN_ARROW = f"{GREEN}↳{ANSI_RESET}"
DIM_RED_ARROW = f"{DIM_RED}↳{ANSI_RESET}"


def _indent_message(message: str, depth: int) -> str:
    if depth <= 0:
        return message
    indent = TRACE_INDENT * depth
    return indent + message.replace("\n", f"\n{indent}")


def _replace_path_prefix(text: str, prefix: str, replacement: str) -> str:
    if not prefix or prefix == "/":
        return text
    text = text.replace(prefix + os.sep, replacement + os.sep)
    return text.replace(prefix, replacement)


def _shorten_paths(text: str) -> str:
    text = _replace_path_prefix(text, CURRENT_WORKING_DIR, ".")
    if CURRENT_HOME and CURRENT_HOME != "/":
        text = _replace_path_prefix(text, CURRENT_HOME, "~")
    return text


def _active_trace_depth() -> int:
    trace_depth_override = TRACE_DEPTH_OVERRIDE.get()
    if trace_depth_override is not None:
        return max(trace_depth_override, 0)
    return max(TRACE_DEPTH.get(), 0)


def _abxpkg_log_record_factory(*args: Any, **kwargs: Any) -> py_logging.LogRecord:
    record = ORIGINAL_LOG_RECORD_FACTORY(*args, **kwargs)
    if record.name != LOGGER_NAME and not record.name.startswith(f"{LOGGER_NAME}."):
        return record
    if getattr(record, "abx_trace_indented", False):
        return record
    message = _shorten_paths(record.getMessage())
    trace_depth = _active_trace_depth() if logger.isEnabledFor(py_logging.DEBUG) else 0
    if trace_depth > 0:
        message = _indent_message(message, trace_depth)
    record.msg = message
    record.args = ()
    record.abx_trace_indented = True
    return record


py_logging.setLogRecordFactory(_abxpkg_log_record_factory)


def log_with_trace_depth(
    method_logger: py_logging.Logger,
    level: int,
    trace_depth: int,
    msg: str,
    *args: Any,
) -> None:
    token = TRACE_DEPTH_OVERRIDE.set(max(trace_depth, 0))
    try:
        method_logger.log(level, msg, *args)
    finally:
        TRACE_DEPTH_OVERRIDE.reset(token)


def get_logger(name: str | None = None) -> py_logging.Logger:
    if not name or name == LOGGER_NAME:
        return logger
    return py_logging.getLogger(name)


def normalize_log_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    normalized = getattr(py_logging, str(level).upper(), None)
    if isinstance(normalized, int):
        return normalized
    raise ValueError(f"Unsupported log level: {level}")


def configure_logging(
    level: int | str = py_logging.WARNING,
    handler: py_logging.Handler | None = None,
    fmt: str = DEFAULT_LOG_FORMAT,
    datefmt: str | None = None,
    propagate: bool = False,
    replace_handlers: bool = False,
) -> py_logging.Logger:
    package_logger = get_logger()
    package_logger.setLevel(normalize_log_level(level))
    package_logger.propagate = propagate

    if replace_handlers:
        package_logger.handlers.clear()

    if handler is None:
        handler = py_logging.StreamHandler()

    if handler.formatter is None:
        handler.setFormatter(py_logging.Formatter(fmt=fmt, datefmt=datefmt))

    if handler not in package_logger.handlers:
        package_logger.addHandler(handler)

    return package_logger


def configure_rich_logging(
    level: int | str = py_logging.WARNING,
    console: "Console | None" = None,
    fmt: str = "%(message)s",
    datefmt: str | None = None,
    propagate: bool = False,
    replace_handlers: bool = False,
    rich_tracebacks: bool = True,
    markup: bool = False,
    show_time: bool = True,
    show_level: bool = True,
    show_path: bool = False,
    omit_repeated_times: bool = False,
    keywords: list[str] | None = None,
    highlighter: Any | None = None,
) -> py_logging.Logger:
    if not RICH_INSTALLED:
        raise RuntimeError(
            'rich is not installed, install "abxpkg[rich]" to enable rich logging',
        )

    from rich.highlighter import ReprHighlighter
    from rich.logging import RichHandler

    handler = RichHandler(
        console=console,
        rich_tracebacks=rich_tracebacks,
        markup=markup,
        show_time=show_time,
        show_level=show_level,
        show_path=show_path,
        omit_repeated_times=omit_repeated_times,
        keywords=keywords,
        highlighter=highlighter if highlighter is not None else ReprHighlighter(),
    )
    return configure_logging(
        level=level,
        handler=handler,
        fmt=fmt,
        datefmt=datefmt,
        propagate=propagate,
        replace_handlers=replace_handlers,
    )


def format_command(cmd: list[str] | tuple[str, ...]) -> str:
    rendered = shlex.join(str(part) for part in cmd)
    return _shorten_paths(rendered)


def format_provider(provider: Any) -> str:
    return f"{provider.__class__.__name__}()"


def format_loaded_binary_line(
    version: Any,
    abspath: Path | str,
    provider_name: str,
    bin_name: str | None = None,
) -> str:
    suffix = f" {bin_name}" if bin_name else ""
    rendered_version = str(version).ljust(12)
    rendered_abspath = str(abspath)
    if " " in rendered_abspath:
        rendered_abspath = f'"{rendered_abspath}"'
    return _shorten_paths(
        f"{rendered_version} {rendered_abspath} ({provider_name}){suffix}",
    )


def format_loaded_binary(
    action: str,
    abspath: Path | str,
    version: Any,
    provider: Any,
    bin_name: str | None = None,
) -> str:
    provider_name = getattr(provider, "name", None) or format_provider(provider)
    return format_loaded_binary_line(version, abspath, provider_name, bin_name)


def _truncate_middle(text: str, keep_start: int = 8, keep_end: int = 8) -> str:
    if len(text) <= keep_start + keep_end + 3:
        return text
    return text[:keep_start] + "..." + text[-keep_end:]


def format_named_value(value: Any) -> str:
    name = getattr(value, "name", None)
    if hasattr(value, "loaded_abspath") and hasattr(value, "loaded_version"):
        loaded_sha256 = getattr(value, "loaded_sha256", None)
        loaded_mtime = getattr(value, "loaded_mtime", None)
        loaded_euid = getattr(value, "loaded_euid", None)
        return _shorten_paths(
            f"{value.__class__.__name__}("
            f"{summarize_value(name, 80)}, "
            f"abspath={summarize_value(getattr(value, 'loaded_abspath', None), 120)}, "
            f"version={summarize_value(getattr(value, 'loaded_version', None), 80)}, "
            f"sha256={_truncate_middle(str(loaded_sha256)) if loaded_sha256 else None!r}, "
            f"mtime={loaded_mtime!r}, "
            f"euid={loaded_euid!r}"
            f")",
        )
    return _shorten_paths(
        f"{value.__class__.__name__}({summarize_value(name, 80)})",
    )


def _truncate_rendered(rendered: str, max_length: int) -> str:
    if len(rendered) <= max_length:
        return rendered
    if rendered and rendered[0] == rendered[-1] and rendered[0] in {"'", '"'}:
        return rendered[0] + _truncate_middle(rendered[1:-1]) + rendered[-1]
    return _truncate_middle(rendered)


def _format_subprocess_payload_lines(
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> list[str]:
    stdout_rendered = format_subprocess_output(stdout, None)
    stderr_rendered = format_subprocess_output(None, stderr)
    lines: list[str] = []
    if stderr_rendered:
        lines.extend(
            "  " + DIM_RED + ">" + ANSI_RESET + " " + _shorten_paths(line)
            for line in stderr_rendered.splitlines()
        )
    if stdout_rendered:
        lines.extend(
            "  > " + _shorten_paths(line) for line in stdout_rendered.splitlines()
        )
    return lines


def format_completed_process(proc: subprocess.CompletedProcess) -> str:
    args = proc.args
    if isinstance(args, (list, tuple)):
        rendered_items = [summarize_value(arg, 400) for arg in args]
        rendered_args = "[" + ", ".join(rendered_items) + "]"
    else:
        rendered_args = summarize_value(args, 400)

    lines = [
        _shorten_paths(
            f"CompletedProcess({rendered_args}, returncode={proc.returncode})",
        ),
    ]

    lines.extend(_format_subprocess_payload_lines(proc.stdout, proc.stderr))

    return "\n".join(lines)


def summarize_value(value: Any, max_length: int = 200) -> str:
    """Render a concise logging-safe representation for arbitrary values.

    Logging must never raise while formatting debug output. Some provider or
    model objects can have broken `repr()` implementations or properties that
    raise on access, so this degrades to `ClassName(...)` instead of letting
    logging itself become the failure.
    """
    try:
        nested_max_length = max(40, min(max_length, 400))
        if isinstance(value, Path):
            rendered = repr(str(value))
        elif isinstance(value, subprocess.CompletedProcess):
            return format_completed_process(value)
        elif isinstance(value, SemVer):
            rendered = f"SemVer([{', '.join(str(chunk) for chunk in value)}])"
        elif isinstance(value, (str, int, float, bool, type(None))):
            rendered = repr(value)
        elif isinstance(value, dict):
            items = ", ".join(
                f"{summarize_value(key, nested_max_length)}: {summarize_value(val, nested_max_length)}"
                for key, val in list(value.items())[:4]
            )
            rendered = f"{{{items}}}"
        elif isinstance(value, list):
            rendered_items = ", ".join(
                summarize_value(item, nested_max_length) for item in list(value)[:4]
            )
            rendered = f"[{rendered_items}]"
        elif isinstance(value, tuple):
            rendered_items = ", ".join(
                summarize_value(item, nested_max_length) for item in list(value)[:4]
            )
            trailing_comma = "," if len(value) == 1 else ""
            rendered = f"({rendered_items}{trailing_comma})"
        elif isinstance(value, set):
            rendered_items = ", ".join(
                summarize_value(item, nested_max_length) for item in list(value)[:4]
            )
            rendered = f"{{{rendered_items}}}"
        elif isinstance(value, frozenset):
            rendered_items = ", ".join(
                summarize_value(item, nested_max_length) for item in list(value)[:4]
            )
            rendered = f"frozenset({{{rendered_items}}})"
        elif callable(value):
            callable_name = getattr(value, "__qualname__", None) or getattr(
                value,
                "__name__",
                None,
            )
            rendered = (
                f"{value.__class__.__name__}({callable_name})"
                if callable_name
                else f"{value.__class__.__name__}(...)"
            )
        elif hasattr(value, "name"):
            rendered = format_named_value(value)
            max_length = max(max_length, 400)
        else:
            rendered = repr(value)
    except Exception:
        rendered = f"{value.__class__.__name__}(...)"

    return _truncate_rendered(_shorten_paths(rendered), max_length)


def format_raised_exception(err: Exception) -> str:
    message = _shorten_paths(format_exception_with_output(err)).strip()
    raised = f"{DIM_RED}raised{ANSI_RESET}"
    if not message:
        return f"{DIM_RED_ARROW} {raised} {err.__class__.__name__}()"
    lines = [
        line.replace("\\", "\\\\").replace('"', '\\"') for line in message.splitlines()
    ]
    if len(lines) == 1:
        return f'{DIM_RED_ARROW} {raised} {err.__class__.__name__}("{lines[0]}")'
    rendered_lines = [f'{DIM_RED_ARROW} {raised} {err.__class__.__name__}("{lines[0]}']
    rendered_lines.extend(f"{TRACE_INDENT}{line}" for line in lines[1:-1])
    rendered_lines.append(f'{TRACE_INDENT}{lines[-1]}")')
    return "\n".join(rendered_lines)


def _display_parameter_name(name: str) -> str:
    return {
        "loaded_version": "version",
        "loaded_sha256": "sha256",
        "loaded_mtime": "mtime",
        "loaded_euid": "euid",
    }.get(name, name)


def _summarize_parameter_value(name: str, value: Any) -> str:
    if name == "sha256":
        max_length = 21
    elif name == "cmd":
        max_length = 400
    else:
        max_length = 80
    return summarize_value(value, max_length)


def _rendered_method_parameter_names(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> set[str]:
    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(*args, **kwargs)
    except Exception:
        return set()

    owner = args[0] if args else None
    is_binary_owner = (
        owner is not None
        and not isinstance(owner, type)
        and type(
            owner,
        ).__name__
        == "Binary"
    )
    binary_owner = cast(Any, owner) if is_binary_owner else None
    owner_field_names = (
        set(type(binary_owner).model_fields) if binary_owner is not None else set()
    )
    owner_explicit_fields = (
        binary_owner.model_fields_set if binary_owner is not None else set()
    )
    owner_fields = type(binary_owner).model_fields if binary_owner is not None else {}
    rendered_names: set[str] = set()
    for name, parameter in signature.parameters.items():
        if name in {"self", "cls"}:
            continue
        if name in bound.arguments:
            value = bound.arguments[name]
            if (
                is_binary_owner
                and name in owner_field_names
                and name in owner_explicit_fields
                and name != "binproviders"
                and parameter.default is not inspect.Signature.empty
                and value == parameter.default
            ):
                owner_value = binary_owner.__dict__.get(name)
                owner_default = (
                    owner_fields[name].default_factory()
                    if owner_fields[name].default_factory is not None
                    else owner_fields[name].default
                )
                if owner_value != owner_default:
                    value = owner_value
        elif (
            is_binary_owner
            and name in owner_field_names
            and name in owner_explicit_fields
            and name != "binproviders"
        ):
            value = binary_owner.__dict__.get(name)
        else:
            continue
        if (
            parameter.default is not inspect.Signature.empty
            and value == parameter.default
        ):
            continue
        rendered_names.add(name)

    return rendered_names


def _binary_owner_fields(owner: Any, rendered_method_names: set[str]) -> str:
    name = summarize_value(owner.__dict__.get("name"), 80)
    explicit_fields = owner.model_fields_set
    rendered_parts = [name]

    for field_name in type(owner).model_fields:
        if field_name in {
            "name",
            "description",
            "overrides",
            "loaded_binprovider",
            "loaded_abspath",
            "loaded_version",
            "loaded_sha256",
            "loaded_mtime",
            "loaded_euid",
        }:
            continue
        if field_name in rendered_method_names and field_name != "binproviders":
            continue
        if field_name not in explicit_fields:
            continue

        field = type(owner).model_fields[field_name]
        value = owner.__dict__.get(field_name)
        default = (
            field.default_factory()
            if field.default_factory is not None
            else field.default
        )
        if value == default:
            continue

        if field_name == "binproviders":
            provider_names = ", ".join(
                provider.__class__.__name__ for provider in value or ()
            )
            rendered_parts.append(f"binproviders=[{provider_names}]")
            continue

        rendered_parts.append(f"{field_name}={summarize_value(value, 80)}")

    return ", ".join(rendered_parts)


def _format_method_call(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(*args, **kwargs)
    except Exception:
        rendered_args = [summarize_value(arg, 80) for arg in args]
        rendered_kwargs = [
            f"{key}={summarize_value(value, 80)}" for key, value in kwargs.items()
        ]
        return ", ".join([*rendered_args, *rendered_kwargs])

    owner = args[0] if args else None
    is_binary_owner = (
        owner is not None
        and not isinstance(owner, type)
        and type(
            owner,
        ).__name__
        == "Binary"
    )
    binary_owner = cast(Any, owner) if is_binary_owner else None
    owner_field_names = (
        set(type(binary_owner).model_fields) if binary_owner is not None else set()
    )
    owner_explicit_fields = (
        binary_owner.model_fields_set if binary_owner is not None else set()
    )
    owner_fields = type(binary_owner).model_fields if binary_owner is not None else {}
    rendered_parts: list[str] = []
    for name, parameter in signature.parameters.items():
        if name in bound.arguments:
            value = bound.arguments[name]
            if (
                is_binary_owner
                and name in owner_field_names
                and name in owner_explicit_fields
                and name != "binproviders"
                and parameter.default is not inspect.Signature.empty
                and value == parameter.default
            ):
                owner_value = binary_owner.__dict__.get(name)
                owner_default = (
                    owner_fields[name].default_factory()
                    if owner_fields[name].default_factory is not None
                    else owner_fields[name].default
                )
                if owner_value != owner_default:
                    value = owner_value
        elif (
            is_binary_owner
            and name in owner_field_names
            and name in owner_explicit_fields
            and name != "binproviders"
        ):
            value = binary_owner.__dict__.get(name)
        else:
            continue
        display_name = _display_parameter_name(name)

        if name in {"self", "cls"}:
            continue

        if (
            parameter.default is not inspect.Signature.empty
            and value == parameter.default
        ):
            continue

        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            rendered_parts.extend(
                summarize_value(item, 80) for item in cast(tuple[Any, ...], value)
            )
            continue
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            rendered_parts.extend(
                f"{key}={summarize_value(val, 80)}"
                for key, val in cast(dict[str, Any], value).items()
            )
            continue
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ) and display_name in {"bin_name", "binary_name", "provider_name", "name"}:
            rendered_parts.append(_summarize_parameter_value(display_name, value))
            continue

        rendered_parts.append(
            f"{display_name}={_summarize_parameter_value(display_name, value)}",
        )

    return ", ".join(rendered_parts)


def _runtime_qualname(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    func_name = getattr(func, "__name__", type(func).__name__)
    if args:
        owner = args[0]
        if not isinstance(owner, type) and type(owner).__name__ == "Binary":
            rendered_method_names = _rendered_method_parameter_names(func, args, kwargs)
            return f"Binary({_binary_owner_fields(owner, rendered_method_names)}).{func_name}"
        owner_name = (
            owner.__name__
            if isinstance(owner, type)
            else getattr(owner.__class__, "__name__", None)
        )
        if owner_name:
            if owner_name.endswith("Provider"):
                owner_emoji_attr = cast(
                    Any,
                    owner.__class__,
                ).__private_attributes__.get(
                    "_log_emoji",
                )
                owner_emoji = (
                    owner_emoji_attr.default if owner_emoji_attr is not None else "📦"
                )
                return f"{owner_emoji} {owner_name}.{func_name}"
            return f"{owner_name}.{func_name}"
    return getattr(func, "__qualname__", func_name)


def _bound_method_args(
    func: Callable[..., Any],
    args: tuple[Any, ...],
) -> tuple[Any, ...]:
    qualname = getattr(func, "__qualname__", "")
    if args and "." in qualname and "<locals>" not in qualname:
        return args[1:]
    return args


def log_method_call(
    level: int = py_logging.DEBUG,
    include_result: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            method_logger = get_logger(func.__module__)
            func_name = getattr(func, "__name__", type(func).__name__)
            qualname = _runtime_qualname(func, args, kwargs)
            should_trace = not (
                func_name.startswith("_")
                or func_name == "get_binprovider"
                or func_name == "get_provider_with_overrides"
            )
            trace_depth = TRACE_DEPTH.get()
            token = TRACE_DEPTH.set(trace_depth + 1) if should_trace else None
            should_log_level = should_trace and method_logger.isEnabledFor(level)
            should_log_error = (
                should_trace
                and trace_depth == 0
                and method_logger.isEnabledFor(py_logging.DEBUG)
            )
            rendered_call: str | None = None
            if should_log_level:
                rendered_call = _format_method_call(func, args, kwargs)
                log_with_trace_depth(
                    method_logger,
                    level,
                    trace_depth,
                    "%s(%s)",
                    qualname,
                    rendered_call,
                )
            try:
                result = func(*args, **kwargs)
            except Exception as err:
                if should_log_error:
                    log_with_trace_depth(
                        method_logger,
                        py_logging.ERROR,
                        trace_depth,
                        "%s",
                        format_raised_exception(err),
                    )
                raise
            finally:
                if token is not None:
                    TRACE_DEPTH.reset(token)
            if should_log_level and include_result:
                if rendered_call is None:
                    rendered_call = _format_method_call(func, args, kwargs)
                arrow = "↳"
                if isinstance(result, subprocess.CompletedProcess):
                    arrow = GREEN_ARROW if result.returncode == 0 else DIM_RED_ARROW
                elif getattr(result, "is_valid", False):
                    arrow = GREEN_ARROW
                log_with_trace_depth(
                    method_logger,
                    level,
                    trace_depth,
                    "%s %s",
                    arrow,
                    summarize_value(result),
                )
            return result

        setattr(wrapper, "__abx_log_level__", level)
        setattr(wrapper, "__abx_log_include_result__", include_result)
        return wrapper

    return decorator


def log_subprocess_output(
    command_logger: py_logging.Logger,
    action: str,
    stdout: str | None,
    stderr: str | None,
    level: int = py_logging.DEBUG,
) -> None:
    payload_lines = _format_subprocess_payload_lines(stdout, stderr)
    if payload_lines:
        command_logger.log(level, "%s:\n%s", action, "\n".join(payload_lines))


def format_subprocess_output(
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> str:
    def _to_str(val: str | bytes | None) -> str:
        if val is None:
            return ""
        return val.decode("utf-8", errors="replace") if isinstance(val, bytes) else val

    return "\n".join(
        part for part in (_to_str(stdout).strip(), _to_str(stderr).strip()) if part
    )


def format_exception_with_output(err: Exception) -> str:
    if isinstance(err, BinaryOperationError) and err.errors:
        summary = str(err).split(" ERRORS=", 1)[0]
        lines = [summary]
        for provider_name, detail in err.errors.items():
            detail_lines = _shorten_paths(str(detail).strip()).splitlines()
            if not detail_lines:
                continue
            lines.append(f"{provider_name}: {detail_lines[0]}")
            lines.extend(f"{TRACE_INDENT}{line}" for line in detail_lines[1:])
        message = "\n".join(lines)
    else:
        message = _shorten_paths(str(err).strip())
    output = format_subprocess_output(
        getattr(err, "stdout", None),
        getattr(err, "stderr", None),
    )
    if output and output not in message:
        return f"{message}\n{output}".strip()
    return message
