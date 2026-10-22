"""Build model guidance and Hermes tool rejection messages."""

import logging
from string import Formatter
from xml.sax.saxutils import escape


_CONTINUE = 'Submit the corrected call in the same turn or continue with independent read operations.'
_PATTERN_REJECTION = 'guiderails.pattern_detected'
DISCARD_MARKER = '[guiderails:discard-tool-result]'
DISCARD_RESULT = f"""\
{_PATTERN_REJECTION}
{DISCARD_MARKER}"""

_CORRECTION = """\
Guiderails correction ({rule_id}): The call was blocked because it violated a rule.
Rule: {description}
How to fix it: {instruction}
{continuation}"""

_WARNING = """\
Guiderails warning ({rule_id}): The call was allowed because the rejection limit was reached.
Rule: {description}
How to fix the next call: {instruction}"""

_MODEL_BLOCK = """\
<guiderails>
{guidance}
</guiderails>"""

_TOOL_REJECTION = """\
{code}
{guidance}{continuation}"""

_DEFAULT_TEMPLATES = {
    'correction': _CORRECTION,
    'warning': _WARNING,
    'continuation': _CONTINUE,
    'ambiguous_file_format': 'Read the file and submit a patch with exact old_string and new_string values.',
    'ambiguous_or_fuzzy_edit': 'Read the file and submit an edit whose search text matches the file exactly.',
    'inspection_rejection': 'Check the tool arguments and inspection requirements, then revise the call.',
    'invalid_tool_payload': 'Check the tool arguments.',
    'human_approval_required': 'Use the /guiderails user command to change rules.',
}
_RULE_FIELDS = frozenset({'rule_id', 'description', 'instruction'})
_TEMPLATE_FIELDS = {'correction': _RULE_FIELDS | {'continuation'}, 'warning': _RULE_FIELDS}
logger = logging.getLogger(__name__)


def validate_templates(value):
    if not isinstance(value, dict):
        logger.error('Invalid guidance_templates: expected a mapping; using defaults')
        return {}
    overrides = {}
    formatter = Formatter()
    for key, template in value.items():
        if key not in _DEFAULT_TEMPLATES:
            logger.error('Unknown guidance_templates entry %r; ignoring it', key)
            continue
        try:
            if not isinstance(template, str) or not template.strip():
                raise ValueError('expected nonempty text')
            if key in _TEMPLATE_FIELDS:
                fields = set()
                for _, field, spec, conversion in formatter.parse(template):
                    if field is None:
                        continue
                    if field not in _TEMPLATE_FIELDS[key] or spec or conversion is not None:
                        raise ValueError('unsupported placeholder or format')
                    fields.add(field)
                if fields != _TEMPLATE_FIELDS[key]:
                    raise ValueError('missing required placeholders')
        except ValueError as exc:
            logger.error('Invalid guidance_templates entry %r: %s; using default', key, exc)
        else:
            overrides[key] = template
    return overrides


class GuidanceFactory:
    def __init__(self, overrides=None):
        self._templates = _DEFAULT_TEMPLATES if not overrides else _DEFAULT_TEMPLATES | overrides

    def correction(self, rule_id: str, description: str, instruction: str) -> str:
        return self._templates['correction'].format(
            rule_id=rule_id, description=description, instruction=instruction,
            continuation=self._templates['continuation'],
        )

    def warning(self, rule_id: str, description: str, instruction: str) -> str:
        return self._templates['warning'].format(rule_id=rule_id, description=description, instruction=instruction)

    @staticmethod
    def model_block(guidance: str) -> str:
        return _MODEL_BLOCK.format(guidance=escape(guidance))

    def _tool_rejection(self, code: str, guidance: str) -> dict[str, str]:
        message = _TOOL_REJECTION.format(code=code, guidance=f'{guidance}\n' if guidance else '',
                                         continuation=self._templates['continuation'])
        return {'action': 'block', 'message': message}

    def pattern_rejection(self, instruction: str, *, discard_result: bool) -> dict[str, str]:
        if discard_result:
            return {'action': 'block', 'message': DISCARD_RESULT}
        return self._tool_rejection(_PATTERN_REJECTION, instruction)

    def inspection_rejection(self, code: str) -> dict[str, str]:
        key = code if code in ('ambiguous_file_format', 'ambiguous_or_fuzzy_edit') else 'inspection_rejection'
        return self._tool_rejection(code, self._templates[key])

    def invalid_tool_payload(self) -> dict[str, str]:
        return self._tool_rejection('guiderails.invalid_tool_payload', self._templates['invalid_tool_payload'])

    def human_approval_required(self) -> dict[str, str]:
        return self._tool_rejection('human_approval_command_required', self._templates['human_approval_required'])


DEFAULT_GUIDANCE = GuidanceFactory()
