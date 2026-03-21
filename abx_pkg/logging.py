__package__ = "abx_pkg"

import functools
import logging as py_logging
import shlex
from contextvars import ContextVar

from pathlib import Path
from typing import Any
from collections.abc import Callable

from typing_extensions import ParamSpec, TypeVar

LOGGER_NAME = "abx_pkg"
DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
RICH_INSTALLED = False

try:
    from rich.console import Console
    from rich.highlighter import ReprHighlighter
    from rich.logging import RichHandler

    RICH_INSTALLED = True
except ImportError:
    Console = Any  # type: ignore[assignment]
    ReprHighlighter = None
    RichHandler = None

logger = py_logging.getLogger(LOGGER_NAME)
if not any(isinstance(handler, py_logging.NullHandler) for handler in logger.handlers):
    logger.addHandler(py_logging.NullHandler())

P = ParamSpec("P")
R = TypeVar("R")
TRACE_DEPTH: ContextVar[int] = ContextVar("abx_pkg_trace_depth", default=0)


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
    level: int | str = py_logging.INFO,
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
    level: int | str = py_logging.INFO,
    console: Console | None = None,
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
    if RichHandler is None:
        raise RuntimeError(
            'rich is not installed, install "abx-pkg[rich]" to enable rich logging',
        )

    handler = RichHandler(
        console=console,
        rich_tracebacks=rich_tracebacks,
        markup=markup,
        show_time=show_time,
        show_level=show_level,
        show_path=show_path,
        omit_repeated_times=omit_repeated_times,
        keywords=keywords,
        highlighter=highlighter
        if highlighter is not None
        else (ReprHighlighter() if ReprHighlighter is not None else None),
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
    return shlex.join(str(part) for part in cmd)


def format_provider(provider: Any) -> str:
    return f"{provider.__class__.__name__}()"


def format_loaded_binary(
    action: str,
    abspath: Path | str,
    version: Any,
    provider: Any,
) -> str:
    return f"{action} {abspath} v{version} via {format_provider(provider)}"


def format_named_value(value: Any) -> str:
    name = getattr(value, "name", None)
    if hasattr(value, "loaded_abspath") and hasattr(value, "loaded_version"):
        return (
            f"{value.__class__.__name__}("
            f"name={name!r}, "
            f"abspath={getattr(value, 'loaded_abspath', None)!r}, "
            f"version={getattr(value, 'loaded_version', None)!r}, "
            f"sha256={('...' + str(getattr(value, 'loaded_sha256', None))[-6:]) if getattr(value, 'loaded_sha256', None) else None!r}"
            f")"
        )
    return f"{value.__class__.__name__}(name={name!r})"


def summarize_value(value: Any, max_length: int = 200) -> str:
    if isinstance(value, Path):
        rendered = str(value)
    elif isinstance(value, (str, int, float, bool, type(None))):
        rendered = repr(value)
    elif isinstance(value, dict):
        items = ", ".join(
            f"{summarize_value(key, 40)}: {summarize_value(val, 60)}"
            for key, val in list(value.items())[:4]
        )
        rendered = f"{{{items}}}"
    elif isinstance(value, (list, tuple, set, frozenset)):
        rendered_items = ", ".join(
            summarize_value(item, 40) for item in list(value)[:4]
        )
        rendered = f"{type(value).__name__}([{rendered_items}])"
    elif hasattr(value, "name"):
        rendered = format_named_value(value)
    else:
        rendered = repr(value)

    if len(rendered) > max_length:
        return rendered[: max_length - 3] + "..."
    return rendered


def _format_method_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    rendered_args = [summarize_value(arg, 80) for arg in args]
    rendered_kwargs = [
        f"{key}={summarize_value(value, 80)}" for key, value in kwargs.items()
    ]
    return ", ".join([*rendered_args, *rendered_kwargs])


def log_method_call(
    level: int = py_logging.DEBUG,
    include_result: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            method_logger = get_logger(func.__module__)
            should_trace = not (
                func.__name__.startswith("_")
                or func.__name__ == "get_binprovider"
                or func.__name__ == "get_provider_with_overrides"
            )
            rendered_call = _format_method_call(args, kwargs)
            trace_depth = TRACE_DEPTH.get()
            token = TRACE_DEPTH.set(trace_depth + 1) if should_trace else None
            if should_trace and method_logger.isEnabledFor(level):
                method_logger.log(level, "%s(%s)", func.__qualname__, rendered_call)
            try:
                result = func(*args, **kwargs)
            except Exception as err:
                if (
                    should_trace
                    and trace_depth == 0
                    and method_logger.isEnabledFor(py_logging.ERROR)
                ):
                    method_logger.error(
                        "%s(%s) raised %r",
                        func.__qualname__,
                        rendered_call,
                        err,
                    )
                raise
            finally:
                if token is not None:
                    TRACE_DEPTH.reset(token)
            if should_trace and include_result and method_logger.isEnabledFor(level):
                method_logger.log(
                    level,
                    "%s(%s) returned %s",
                    func.__qualname__,
                    rendered_call,
                    summarize_value(result),
                )
            return result

        return wrapper

    return decorator


def log_subprocess_error(
    command_logger: py_logging.Logger,
    action: str,
    stdout: str | None,
    stderr: str | None,
) -> None:
    trimmed_stdout = (stdout or "").strip()
    trimmed_stderr = (stderr or "").strip()
    if trimmed_stdout:
        command_logger.error("%s stdout: %s", action, trimmed_stdout)
    if trimmed_stderr:
        command_logger.error("%s stderr: %s", action, trimmed_stderr)


def format_subprocess_output(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(
        part for part in ((stdout or "").strip(), (stderr or "").strip()) if part
    )


def format_exception_with_output(err: Exception) -> str:
    message = str(err).strip()
    output = format_subprocess_output(
        getattr(err, "stdout", None),
        getattr(err, "stderr", None),
    )
    if output and output not in message:
        return f"{message}\n{output}".strip()
    return message
