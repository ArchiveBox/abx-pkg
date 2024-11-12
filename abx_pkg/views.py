# pip install django-admin-data-views

from django.http import HttpRequest
from django.utils.html import mark_safe

from admin_data_views.typing import TableContext, ItemContext
from admin_data_views.utils import render_with_table_view, render_with_item_view, ItemLink

from .binary import Binary


def get_all_binaries() -> list[Binary]:
    """Override this function implement getting the list of binaries to render"""
    return []

def get_binary(name: str) -> Binary:
    """Override this function implement getting the list of binaries to render"""

    from . import settings

    for binary in settings.ABX_PKG_GET_ALL_BINARIES():
        if binary.name == key:
            return binary
    return None



@render_with_table_view
def binaries_list_view(request: HttpRequest, **kwargs) -> TableContext:

    assert request.user.is_superuser, 'Must be a superuser to view configuration settings.'

    from . import settings

    rows = {
        "Binary": [],
        "Found Version": [],
        "Provided By": [],
        "Found Abspath": [],
        "Overrides": [],
        "Description": [],
    }

    for binary in settings.get_all_abx_pkg_binaries():
        binary = binary.load_or_install()

        rows['Binary'].append(ItemLink(binary.name, key=binary.name))
        rows['Found Version'].append(binary.loaded_version)
        rows['Provided By'].append(binary.loaded_binprovider)
        rows['Found Abspath'].append(binary.loaded_abspath)
        rows['Overrides'].append(str(binary.overrides))
        rows['Description'].append(binary.description)

    return TableContext(
        title="Binaries",
        table=rows,
    )

@render_with_item_view
def binary_detail_view(request: HttpRequest, key: str, **kwargs) -> ItemContext:

    assert request.user.is_superuser, 'Must be a superuser to view configuration settings.'

    from . import settings

    binary = settings.get_abx_pkg_binary(key)

    assert binary, f'Could not find a binary matching the specified name: {key}'

    binary = binary.load_or_install()

    return ItemContext(
        slug=key,
        title=key,
        data=[
            {
                "name": binary.name,
                "description": binary.description,
                "fields": {
                    'binprovider': binary.loaded_provider,
                    'abspath': binary.loaded_abspath,
                    'version': binary.loaded_version,
                    'is_script': binary.is_script,
                    'is_executable': binary.is_executable,
                    'is_valid': binary.is_valid,
                    'overrides': str(binary.overrides),
                    'providers': str(binary.binproviders_supported),
                },
                "help_texts": {
                    # TODO
                },
            },
        ],
    )
