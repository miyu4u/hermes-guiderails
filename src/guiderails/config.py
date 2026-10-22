"""Validated v1 profile settings."""
from dataclasses import dataclass
from functools import cached_property
import json

import jsonschema

from .engine import Limits
from .fileio import FileIOError, read_text
from .guidance import DEFAULT_GUIDANCE, GuidanceFactory, validate_templates
from .rules import PatternError, load_yaml


MAX_CONFIG_BYTES = 64 * 1024
DEFAULTS = {
    'schema_version': 1, 'mode': 'strict', 'effect_policy': 'pattern', 'context_mode': 'discard',
    'max_rejections': 3,
    'display': {'on_interrupt': 'mark'},
    'retry': {'max_retries': 3, 'max_turn_retries': 6, 'max_session_blocks': 3,
              'max_elapsed_seconds': 120},
    'limits': {'max_candidate_bytes': 1048576, 'max_rules': 1000, 'max_pattern_chars': 4096,
               'max_hits': 10000, 'regex_timeout_ms': 20, 'max_inspection_ms': 1000,
               'compile_timeout_seconds': 2, 'diff_timeout_ms': 1000},
    'logging': {'rotate_bytes': 10485760, 'rotate_utc_daily': True, 'retention_days': 30,
                'max_archive_bytes': 209715200, 'excerpts': False, 'max_outbox': 10000,
                'excerpt_chars': 160, 'excerpt_retention_days': 1},
    'semantic': {'enabled': False},
    'guidance_templates': {},
}


def object_schema(properties, required=()):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': list(required)}


def positive_integer(maximum):
    return {'type': 'integer', 'minimum': 1, 'maximum': maximum}


CONFIG_SCHEMA = object_schema({
    'schema_version': {'type': 'integer', 'const': 1},
    'mode': {'enum': ['strict', 'observe']},
    'effect_policy': {'enum': ['pattern', 'verified']},
    'max_rejections': {'type': 'integer', 'minimum': 0},
    'context_mode': {'enum': ['discard', 'keep']},
    'display': object_schema({'on_interrupt': {'enum': ['mark', 'retract']}}),
    'retry': object_schema({'max_retries': {'type': 'integer', 'minimum': 0, 'maximum': 100},
                            'max_turn_retries': {'type': 'integer', 'minimum': 0, 'maximum': 1000},
                            'max_session_blocks': {'type': 'integer', 'minimum': 0, 'maximum': 100},
                            'max_elapsed_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 86400}}),
    'limits': object_schema({name: (positive_integer({'max_candidate_bytes': 16777216, 'max_rules': 1000,
                                                     'max_pattern_chars': 4096, 'max_hits': 10000}[name])
                                   if name in ('max_candidate_bytes', 'max_rules', 'max_pattern_chars', 'max_hits')
                                   else {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 60000}) for name in DEFAULTS['limits']}),
    'logging': object_schema({'rotate_bytes': positive_integer(1073741824), 'rotate_utc_daily': {'type': 'boolean'},
                              'retention_days': positive_integer(3650), 'max_archive_bytes': positive_integer(10737418240),
                              'excerpts': {'type': 'boolean'},
                              'max_outbox': {'type': 'integer', 'minimum': 2, 'maximum': 1000000},
                              'excerpt_chars': positive_integer(1024), 'excerpt_retention_days': positive_integer(30)}),
    'semantic': object_schema({'enabled': {'const': False}}),
    # Invalid overrides fall back individually instead of rejecting the profile.
    'guidance_templates': {},
}, required=['schema_version']) | {'$schema': 'https://json-schema.org/draft/2020-12/schema'}


@dataclass(frozen=True)
class RuntimeConfig:
    document: str

    @classmethod
    def from_profile(cls, root):
        if any(path.is_symlink() for path in (root, *root.parents)):
            raise PatternError('symlink_management_root')
        try:
            text = read_text(root / 'config.yaml', MAX_CONFIG_BYTES)
        except FileNotFoundError:
            text = 'schema_version: 1'
        except (FileIOError, OSError, UnicodeError) as exc:
            raise PatternError('invalid_profile_config_path') from exc
        return cls.from_yaml(text)

    @classmethod
    def from_yaml(cls, text):
        value = load_yaml(text, max_bytes=MAX_CONFIG_BYTES)
        try:
            jsonschema.Draft202012Validator(CONFIG_SCHEMA).validate(value)
            if 'max_rejections' in value and type(value['max_rejections']) is not int:
                raise ValueError('max_rejections_must_be_integer')
            merged = json.loads(json.dumps(DEFAULTS))
            for key, setting in value.items():
                if key == 'guidance_templates':
                    merged[key] = validate_templates(setting)
                elif isinstance(setting, dict):
                    merged[key].update(setting)
                else:
                    merged[key] = setting
            return cls(json.dumps(merged, allow_nan=False, sort_keys=True))
        except (jsonschema.ValidationError, ValueError) as exc:
            raise PatternError('invalid_runtime_config') from exc

    def as_dict(self):
        return json.loads(self.document)

    @cached_property
    def guidance(self):
        overrides = self.as_dict()['guidance_templates']
        return GuidanceFactory(overrides) if overrides else DEFAULT_GUIDANCE

    @property
    def limits(self):
        return Limits(**self.as_dict()['limits'])

    @property
    def mode(self):
        return self.as_dict()['mode']

    @property
    def effect_policy(self):
        return self.as_dict()['effect_policy']

    @property
    def max_rejections(self):
        return self.as_dict()['max_rejections']

    @property
    def context_mode(self):
        return self.as_dict()['context_mode']

    @property
    def display_on_interrupt(self):
        return self.as_dict()['display']['on_interrupt']

    @property
    def max_retries(self):
        return self.as_dict()['retry']['max_retries']

    @property
    def max_session_blocks(self):
        return self.as_dict()['retry']['max_session_blocks']

    @property
    def max_turn_retries(self):
        return self.as_dict()['retry']['max_turn_retries']
