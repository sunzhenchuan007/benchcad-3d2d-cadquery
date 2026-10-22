"""Small native function-call contract; execution remains controller-owned."""
import json

PROTOCOL = 'native-tools/1'
GUIDANCE = (
    'Use exactly one native tool call per decision. Use execute_python with a code string '
    'for execution, or submit with an empty object when the required file is saved and ready. '
    'Do not return Markdown code blocks as actions. submit validates the saved artifact; '
    'it does not execute model-written code or bypass validation. Tool errors return feedback '
    'within the original budget. Plain text alone never executes or submits.'
)
TOOLS = [
    {'type': 'function', 'name': 'execute_python', 'strict': True,
     'description': 'Run Python in the bounded CAD container. Files persist; variables do not. '
                    'Returns stdout, stderr, exit status and new/changed PNG images saved at '
                    'workspace root or directly in output/. Never submits the result.',
     'parameters': {'type': 'object', 'properties': {
         'code': {'type': 'string', 'description': 'Complete Python source to execute.'}},
         'required': ['code'], 'additionalProperties': False}},
    {'type': 'function', 'name': 'submit', 'strict': True,
     'description': 'Validate and freeze the required artifact already saved at the task path. '
                    'No code or path arguments. Failed validation returns a repairable error.',
     'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}},
]


def parse_tool_action(calls):
    if len(calls) != 1:
        raise ValueError('Use exactly one tool call per decision; none of these calls executed.')
    call = calls[0]
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate tool argument')
            result[key] = value
        return result
    try:
        args = json.loads(call['arguments'], object_pairs_hook=unique_pairs)
    except (ValueError, TypeError):
        raise ValueError('Tool arguments must be a valid JSON object.') from None
    if not isinstance(args, dict):
        raise ValueError('Tool arguments must be a JSON object.')
    if call['name'] == 'submit' and not args:
        return 'submit', ''
    if (call['name'] == 'execute_python' and set(args) == {'code'}
            and isinstance(args['code'], str) and args['code'].strip()):
        return 'python', args['code']
    raise ValueError('Allowed calls: execute_python({"code": "Python source"}) or submit({}).')
