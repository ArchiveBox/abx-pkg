from django.contrib import admin

from django_jsonform.widgets import JSONFormWidget
from django_pydantic_field.v2.fields import PydanticSchemaField

from project.models import Dependency


def patch_schema_for_jsonform(schema):
    """recursively patch a schema dictionary in-place to fix any missing properties/keys on objects"""

    # base case: schema is type: "object" with no properties/keys
    if schema.get("type") == "object" and not (
        "properties" in schema or "keys" in schema
    ):
        if "default" in schema and isinstance(schema["default"], dict):
            schema["properties"] = {
                key: {"type": "string", "default": value}
                for key, value in schema["default"].items()
            }
        else:
            schema["properties"] = {}
    elif schema.get("type") == "array" and "items" not in schema:
        if "default" in schema and isinstance(schema["default"], (tuple, list)):
            schema["items"] = {"type": "string", "default": schema["default"]}
        else:
            schema["items"] = {"type": "string", "default": []}

    # recursive case: iterate through all values and process any sub-objects
    for key, value in schema.items():
        if isinstance(value, dict):
            patch_schema_for_jsonform(value)


class PatchedJSONFormWidget(JSONFormWidget):
    def get_schema(self):
        self.schema = super().get_schema()
        patch_schema_for_jsonform(self.schema)
        return self.schema


class DependencyAdmin(admin.ModelAdmin):
    formfield_overrides = {PydanticSchemaField: {"widget": PatchedJSONFormWidget}}


admin.site.register(Dependency, DependencyAdmin)
