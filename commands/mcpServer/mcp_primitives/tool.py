# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.
#
# Adapted from Autodesk's Fusion MCP add-in sample (MIT-licensed).

"""MCP Tool schema for defining tool metadata and input schema."""

from typing import Optional
from .annotations import Annotations


class Tool:
    """MCP Tool schema with a fluent builder interface."""

    def __init__(
        self,
        name: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        input_schema: Optional[dict] = None,
        output_schema: Optional[dict] = None,
        annotations: Optional[Annotations] = None,
        additional_properties: Optional[bool] = None
    ):
        self.name = name
        self.title = title
        self.description = description
        self.input_schema = input_schema or {}
        self.output_schema = output_schema
        self.annotations = annotations
        self.additional_properties = additional_properties

    def reads(self) -> 'Tool':
        """Declare this tool READ-ONLY (does not modify the document/state). Sets readOnlyHint=true."""
        if self.annotations is None:
            self.annotations = Annotations()
        self.annotations.set_read_only(True)
        return self

    def writes(self, destructive: bool = False) -> 'Tool':
        """Declare this tool a WRITE. Sets readOnlyHint=false; destructive=true for a hard-to-reverse
        write (deletes, history-discarding conversions, closing docs) -> destructiveHint=true."""
        if self.annotations is None:
            self.annotations = Annotations()
        self.annotations.set_read_only(False)
        if destructive:
            self.annotations.set_destructive(True)
        return self

    def strict_schema(self) -> 'Tool':
        self.additional_properties = False
        return self

    def add_input_property(self, name: str, property_schema: dict) -> 'Tool':
        if 'properties' not in self.input_schema:
            self.input_schema['properties'] = {}
        self.input_schema['properties'][name] = property_schema
        return self

    def add_required_input(self, property_name: str) -> 'Tool':
        if 'required' not in self.input_schema:
            self.input_schema['required'] = []
        if property_name not in self.input_schema['required']:
            self.input_schema['required'].append(property_name)
        return self

    def to_dict(self) -> dict:
        result = {'name': self.name}
        if self.title:
            result['title'] = self.title
        if self.description:
            result['description'] = self.description
        if self.input_schema:
            input_schema = self.input_schema.copy()
            if self.additional_properties is not None:
                input_schema['additionalProperties'] = self.additional_properties
            result['inputSchema'] = input_schema
        if self.output_schema:
            result['outputSchema'] = self.output_schema
        if self.annotations:
            result['annotations'] = self.annotations.to_dict()
        return result

    def __str__(self) -> str:
        return f"Tool(name='{self.name}', title='{self.title}')"

    def __repr__(self) -> str:
        return f"Tool(name='{self.name}', title='{self.title}', description='{self.description}')"

    @classmethod
    def create_simple(cls, name: str, description: str, input_type: str = "object", **kwargs) -> 'Tool':
        input_schema = {"type": input_type, "properties": {}, "required": []}
        return cls(name=name, description=description, input_schema=input_schema, **kwargs)

    @classmethod
    def create_with_string_input(
        cls,
        name: str,
        description: str,
        input_param_name: str = "input",
        input_param_description: str = "Input parameter",
        **kwargs
    ) -> 'Tool':
        tool = cls.create_simple(name, description, **kwargs)
        tool.add_input_property(
            input_param_name,
            {"type": "string", "description": input_param_description}
        ).add_required_input(input_param_name)
        return tool
