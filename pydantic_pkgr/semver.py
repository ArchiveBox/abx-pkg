__package__ = 'pydantic_pkgr'

import re
import subprocess
from collections import namedtuple

from typing import Any, Optional, TYPE_CHECKING

from pydantic_core import ValidationError
from pydantic import validate_call

from .base_types import HostBinPath


def is_semver_str(semver: Any) -> bool:
    if isinstance(semver, str):
        return (semver.count('.') == 2 and semver.replace('.', '').isdigit())
    return False

def semver_to_str(semver: tuple[int, int, int] | str) -> str:
    if isinstance(semver, (list, tuple)):
        return '.'.join(str(chunk) for chunk in semver)
    if is_semver_str(semver):
        return semver
    raise ValidationError('Tried to convert invalid SemVer: {}'.format(semver))


SemVerTuple = namedtuple('SemVerTuple', ('major', 'minor', 'patch'), defaults=(0, 0, 0))
SemVerParsableTypes = str | tuple[str | int, ...] | list[str | int]

class SemVer(SemVerTuple):
    major: int
    minor: int = 0
    patch: int = 0

    if TYPE_CHECKING:
        full_text: str | None = ''

    def __new__(cls, *args, full_text=None, **kwargs):
        # '1.1.1'
        if len(args) == 1 and is_semver_str(args[0]):
            result = SemVer.parse(args[0])

        # ('1', '2', '3')
        elif len(args) == 1 and isinstance(args[0], (tuple, list)):
            result = SemVer.parse(args[0])

        # (1, '2', None)
        elif not all(isinstance(arg, (int, type(None))) for arg in args):
            result = SemVer.parse(args)

        # (None)
        elif all(chunk in ('', 0, None) for chunk in (*args, *kwargs.values())):
            result = None

        # 1, 2, 3
        else:
            result = SemVerTuple.__new__(cls, *args, **kwargs)

        if result is not None:
            # add first line as extra hidden metadata so it can be logged without having to re-run version cmd
            result.full_text = full_text or str(result)
        return result

    @classmethod
    def parse(cls, version_stdout: SemVerParsableTypes) -> Optional['SemVer']:
        """
        parses a version tag string formatted like into (major, minor, patch) ints
        'Google Chrome 124.0.6367.208'             -> (124, 0, 6367)
        'GNU Wget 1.24.5 built on darwin23.2.0.'   -> (1, 24, 5)
        'curl 8.4.0 (x86_64-apple-darwin23.0) ...' -> (8, 4, 0)
        '2024.04.09'                               -> (2024, 4, 9)

        """
        # print('INITIAL_VALUE', type(version_stdout).__name__, version_stdout)

        if isinstance(version_stdout, (tuple, list)):
            version_stdout = '.'.join(str(chunk) for chunk in version_stdout)
        elif isinstance(version_stdout, bytes):
            version_stdout = version_stdout.decode()
        elif not isinstance(version_stdout, str):
            version_stdout = str(version_stdout)
        
        # no text to work with, return None immediately
        if not version_stdout.strip():
            # raise Exception('Tried to parse semver from empty version output (is binary installed and available?)')
            return None

        just_numbers = lambda col: '.'.join([chunk for chunk in re.split(r'[\D]', col.lower().strip('v'), maxsplit=10) if chunk.isdigit()][:3])  # split on any non-num character e.g. 5.2.26(1)-release -> ['5', '2', '26', '1', '', '', ...]
        contains_semver = lambda col: (
            col.count('.') in (1, 2, 3)
            and all(chunk.isdigit() for chunk in col.split('.')[:3])  # first 3 chunks can only be nums
        )

        full_text = version_stdout.split('\n')[0].strip()
        first_line_columns = full_text.split()[:5]
        version_columns = list(filter(contains_semver, map(just_numbers, first_line_columns)))
        
        # could not find any column of first line that looks like a version number, despite there being some text
        if not version_columns:
            # raise Exception('Failed to parse semver from version command output: {}'.format(' '.join(first_line_columns)))
            return None

        # take first col containing a semver, and truncate it to 3 chunks (e.g. 2024.04.09.91) -> (2024, 04, 09)
        first_version_tuple = version_columns[0].split('.', 3)[:3]

        # print('FINAL_VALUE', first_version_tuple)

        return cls(*(int(chunk) for chunk in first_version_tuple), full_text=full_text)

    def __str__(self):
        return '.'.join(str(chunk) for chunk in self)


    # Not needed as long as we dont stray any further from a basic NamedTuple
    # if we start overloading more methods or it becomes a fully custom type, then we probably need this:
    # @classmethod
    # def __get_pydantic_core_schema__(cls, source: Type[Any], handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
    #     default_schema = handler(source)
    #     return core_schema.no_info_after_validator_function(
    #         cls.parse,
    #         default_schema,
    #         serialization=core_schema.plain_serializer_function_ser_schema(
    #             lambda semver: str(semver),
    #             info_arg=False,
    #             return_schema=core_schema.str_schema(),
    #         ),
    #     )


# @validate_call
def bin_version(bin_path: HostBinPath, args=("--version",)) -> SemVer | None:
    return SemVer(subprocess.run([str(bin_path), *args], stdout=subprocess.PIPE, text=True).stdout.strip())
