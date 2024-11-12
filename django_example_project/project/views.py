from abx_pkg.binary import Binary


def get_all_binaries() -> list[Binary]:
    """Override this function implement getting the list of binaries to render"""
    return [
        Binary(name='bash'),
        Binary(name='python'),
        Binary(name='brew'),
        Binary(name='git'),
    ]

def get_binary(name: str) -> Binary:
    """Override this function implement getting the list of binaries to render"""

    from abx_pkg import settings

    for binary in settings.get_all_abx_pkg_binaries():
        if binary.name == name:
            return binary
    return None
