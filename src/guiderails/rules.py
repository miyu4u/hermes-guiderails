"""Strict bounded YAML and v1 rule schema validation."""
import json
from pathlib import Path

import jsonschema
import yaml
from yaml.tokens import AliasToken, AnchorToken, TagToken


class PatternError(ValueError):
    """An untrusted pattern set failed validation."""


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise PatternError('duplicate_or_nonstring_key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_yaml(text, *, max_bytes=1048576):
    if not isinstance(text, str) or len(text.encode('utf-8')) > max_bytes:
        raise PatternError('pattern_source_too_large')
    try:
        depth = 0
        for event in yaml.parse(text, Loader=UniqueLoader):
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > 32:
                    raise PatternError('yaml_depth_limit')
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
        if any(isinstance(token, (AliasToken, AnchorToken, TagToken)) for token in yaml.scan(text)):
            raise PatternError('yaml_alias_or_tag')
        return yaml.load(text, Loader=UniqueLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise PatternError('invalid_yaml') from exc


def parse(text, *, allow_empty=False, max_bytes=1048576):
    try:
        data = load_yaml(text, max_bytes=max_bytes)
        schema = json.loads((Path(__file__).parent / 'schemas/pattern-set.v1.schema.json').read_text())
        jsonschema.Draft202012Validator(schema).validate(data)
        if not data['rules'] and not allow_empty:
            raise PatternError('empty_ruleset')
        ids = [rule['id'] for rule in data['rules']]
        if len(set(ids)) != len(ids):
            raise PatternError('duplicate_rule_id')
        return data
    except (yaml.YAMLError, jsonschema.ValidationError, RecursionError) as exc:
        raise PatternError('invalid_pattern_schema') from exc
