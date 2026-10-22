import unittest

from guiderails.config import RuntimeConfig
from guiderails.rules import PatternError


class ConfigTests(unittest.TestCase):
    def test_max_rejections_requires_a_nonnegative_integer(self):
        self.assertEqual(RuntimeConfig.from_yaml('schema_version: 1').max_rejections, 3)
        for value in (0, 1, 3):
            with self.subTest(value=value):
                config = RuntimeConfig.from_yaml(f'schema_version: 1\nmax_rejections: {value}')
                self.assertEqual(config.max_rejections, value)
        for value in ('-1', 'true', '1.0', '"3"', 'null'):
            with self.subTest(value=value), self.assertRaises(PatternError):
                RuntimeConfig.from_yaml(f'schema_version: 1\nmax_rejections: {value}')

    def test_defaults_and_explicit_settings(self):
        config = RuntimeConfig.from_yaml('schema_version: 1')
        self.assertEqual((config.mode, config.effect_policy), ('strict', 'pattern'))
        self.assertEqual(config.limits.regex_timeout_ms, 20)
        self.assertEqual(config.as_dict()['logging']['excerpt_retention_days'], 1)
        config = RuntimeConfig.from_yaml('schema_version: 1\nmode: observe\neffect_policy: verified')
        self.assertEqual((config.mode, config.effect_policy), ('observe', 'verified'))

    def test_invalid_and_unbounded_configuration_is_rejected(self):
        for text in ('schema_version: 2', 'schema_version: 1\nunknown: 3',
                     'schema_version: 1\nmode: unknown',
                     'schema_version: 1\nlogging: {max_outbox: 1}',
                     'schema_version: 1\nlogging: {max_outbox: 0}',
                     'schema_version: 1\nlimits: {max_hits: 10001}',
                     'schema_version: 1\nlimits: {regex_timeout_ms: 0}',
                     'schema_version: 1\nlimits: {regex_timeout_ms: .nan}',
                     'schema_version: 1\nretry: {max_retries: true}',
                     'schema_version: 1\nsemantic: {enabled: true}',
                     'schema_version: 1\nmode: strict\nmode: observe',
                     'schema_version: 1\n' + ' ' * 65536):
            with self.subTest(text=text[:80]), self.assertRaises(PatternError):
                RuntimeConfig.from_yaml(text)

    def test_minimum_outbox_capacity_is_accepted(self):
        config = RuntimeConfig.from_yaml('schema_version: 1\nlogging: {max_outbox: 2}')
        self.assertEqual(config.as_dict()['logging']['max_outbox'], 2)

    def test_returned_configuration_is_detached(self):
        config = RuntimeConfig.from_yaml('schema_version: 1')
        config.as_dict()['limits']['max_hits'] = 1
        self.assertEqual(config.limits.max_hits, 10000)

    def test_missing_or_malformed_guidance_templates_use_defaults(self):
        args = ('sample.rule', 'Sample rule.', 'Try again.')
        with self.assertNoLogs('guiderails.guidance', level='ERROR'):
            default = RuntimeConfig.from_yaml('schema_version: 1').guidance.correction(*args)
        for setting in (
            'null', '[unexpected]',
            '{correction: 5}', '{correction: " "}',
            '{correction: "{rule_id} {description} {instruction} {continuation"}',
            '{correction: "{rule_id} {description} {instruction}"}',
            '{correction: "{rule_id} {description} {instruction} {continuation} {extra}"}',
            '{correction: "{rule_id.upper} {description} {instruction} {continuation}"}',
            '{correction: "{rule_id} {description!r} {instruction:>50} {continuation}"}',
            '{unknown: "ignored"}',
        ):
            with self.subTest(setting=setting), self.assertLogs('guiderails.guidance', level='ERROR') as logs:
                config = RuntimeConfig.from_yaml(f'schema_version: 1\nguidance_templates: {setting}')
                self.assertEqual(config.guidance.correction(*args), default)
                self.assertEqual(len(logs.records), 1)
                self.assertEqual(logs.records[0].levelname, 'ERROR')
