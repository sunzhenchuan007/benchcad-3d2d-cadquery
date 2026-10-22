"""Native tool contract tests; all model calls are offline fixtures."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from benchcad_runtime.episode import Reply, Execution, Budget, run_episode
from benchcad_runtime.native_tools import PROTOCOL, TOOLS, parse_tool_action
from benchcad_runtime.openai_api import OpenAIResponses, encode_messages, preflight_request_bytes
from benchcad_runtime.local_accounting import estimate_request
from benchcad_runtime.errors import SubmissionRejected
from benchcad_cadquery.cli import system_prompt, configure, parser


def call(name='execute_python', args=None, ident='call_1'):
    return {'type':'function_call','call_id':ident,'name':name,
            'arguments':json.dumps(args if args is not None else {'code':'print(1)'})}


class Model:
    action_protocol = PROTOCOL
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
    def __call__(self, system, messages, timeout):
        self.requests.append((system, json.loads(json.dumps(messages))))
        return next(self.replies)


class NativeToolTests(unittest.TestCase):
    def episode(self, replies, finalizer=None, budget=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        model = Model(replies)
        executor = Mock()
        executor.execute.return_value = Execution(0, stdout='result', images=['snapshot.png'])
        finalizer = finalizer or Mock(return_value='frozen')
        result = run_episode(model,executor,finalizer,'task',Path(tmp.name)/'episode',budget or Budget())
        return result,model,executor,finalizer

    def test_execute_then_submit_no_extra_python_for_submit(self):
        result,model,executor,finalizer = self.episode([
            Reply('',tool_calls=[call()]),Reply('',tool_calls=[call('submit',{},'call_2')])])
        self.assertTrue(result['submitted'])
        self.assertEqual(result['decision_rounds'],2)
        self.assertEqual(result['code_executions'],1)
        self.assertEqual(result['action_protocol'],PROTOCOL)
        executor.execute.assert_called_once()
        finalizer.assert_called_once()
        feedback = model.requests[1][1][-1]
        self.assertEqual(feedback['tool_call_ids'],['call_1'])
        self.assertEqual(feedback['images'],['snapshot.png'])
        self.assertNotIn('```submit',model.requests[0][0])

    def test_multi_call_rejected_and_every_call_gets_result(self):
        result,model,executor,_ = self.episode([
            Reply('',tool_calls=[call(),call('submit',{},'call_2')]),
            Reply('',tool_calls=[call('submit',{},'call_3')])])
        executor.execute.assert_not_called()
        self.assertEqual(result['decision_rounds'],2)
        self.assertEqual(result['events'][0]['event'],'format_error')
        messages=model.requests[1][1]
        wire=encode_messages(messages)
        self.assertEqual([i['call_id'] for i in wire if i.get('type')=='function_call_output'],
                         ['call_1','call_2'])
        self.assertIn('none of these calls executed',messages[-1]['text'])

    def test_plain_fenced_python_is_not_native_action(self):
        result,model,executor,_ = self.episode([
            Reply('```python\nprint(1)\n```'),Reply('',tool_calls=[call('submit',{},'next')])])
        executor.execute.assert_not_called()
        self.assertEqual(result['events'][0]['event'],'format_error')
        self.assertIn('native tool call',model.requests[1][1][-1]['text'])

    def test_incomplete_tool_response_never_executes(self):
        result,model,executor,_ = self.episode([
            Reply('',executable=False,incomplete_reason='max_output_tokens',tool_calls=[call()]),
            Reply('',tool_calls=[call('submit',{},'next')])])
        executor.execute.assert_not_called()
        self.assertEqual(model.requests[1][1][-1]['tool_call_ids'],['call_1'])
        self.assertIn('no code executed',model.requests[1][1][-1]['text'])

    def test_submit_rejection_returns_correlated_error(self):
        finalizer=Mock(side_effect=[SubmissionRejected('Missing STEP'),'frozen'])
        result,model,executor,_ = self.episode([
            Reply('',tool_calls=[call('submit',{})]),Reply('',tool_calls=[call('submit',{},'next')])],finalizer)
        self.assertTrue(result['submitted'])
        self.assertEqual(result['decision_rounds'],2)
        self.assertEqual(result['code_executions'],0)
        feedback=model.requests[1][1][-1]
        self.assertEqual(feedback['tool_call_ids'],['call_1'])
        self.assertIn('Missing STEP',feedback['text'])
        self.assertEqual(feedback['feedback']['rounds_remaining'],59)

    def test_explicit_budget_continuation_keeps_rounds_history_and_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = [100.0]
            class TimedModel(Model):
                max_output_tokens = 500000
                def __call__(self, system, messages, timeout):
                    now[0] += 2
                    return super().__call__(system, messages, timeout)
            incomplete = Reply('', executable=False, response_status='incomplete',
                               incomplete_reason='max_output_tokens', usage={'output_tokens': 32768})
            executor, finalizer = Mock(), Mock(return_value='frozen')
            first = run_episode(TimedModel([incomplete, incomplete]), executor, finalizer,
                                'original task', root/'first', Budget(rounds=30, wall_seconds=1800),
                                clock=lambda: now[0])
            self.assertEqual(first['ended_by'], 'response_budget_exhausted')
            transcript = json.loads((root/'first/transcript.json').read_text())
            resume = {'episode': first, 'messages': transcript, 'parent': 'first',
                      'feedback': {'role':'user', 'text':'Budget increased by operator; continue.', 'images':[]},
                      'allow_budget_extension': True, 'reason': 'User authorized 60 rounds, 7200 seconds, 500000 tokens'}
            model = TimedModel([Reply('', tool_calls=[call('submit', {}, 'finish')], usage={'output_tokens':10})])
            result = run_episode(model, executor, finalizer, 'unused', root/'continued', Budget(),
                                 clock=lambda: now[0], resume=resume)
            self.assertTrue(result['submitted'])
            self.assertEqual(result['decision_rounds'], 3)
            self.assertEqual(result['model_call_attempts'], 3)
            self.assertEqual(result['wall_seconds'], 6)
            self.assertEqual(len(result['usage_records']), 3)
            self.assertEqual(model.requests[0][1][:-1], transcript)
            executor.execute.assert_not_called()
            events = [json.loads(l) for l in (root/'continued/journal.jsonl').read_text().splitlines()]
            revision = next(e for e in events if e['event']=='episode_resumed')
            self.assertEqual(revision['prior_budget']['rounds'], 30)
            self.assertEqual(revision['new_budget']['rounds'], 60)
            self.assertEqual(revision['max_output_tokens'], 500000)

    def test_budget_continuation_requires_explicit_control_and_preserves_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incomplete = Reply('', executable=False, response_status='incomplete',
                               incomplete_reason='max_output_tokens')
            first = run_episode(Model([incomplete, incomplete]), Mock(), Mock(), 'task',
                                root/'first', Budget(rounds=30, wall_seconds=1800))
            transcript = json.loads((root/'first/transcript.json').read_text())
            base = {'episode': first, 'messages': transcript, 'allow_budget_extension':True,
                    'reason':'operator approved', 'feedback':{'role':'user','text':'continue'}}
            cases = [(dict(base, allow_budget_extension=False), Budget()),
                     (dict(base, reason=''), Budget()),
                     (base, Budget(rounds=20)), (base, Budget(execution_seconds=301)),
                     (base, Budget(max_transport_failures=4)),
                     (dict(base, episode=dict(first, submitted=True, bundle='frozen')), Budget())]
            for i, (resume, budget) in enumerate(cases):
                model = Model([])
                with self.subTest(i=i), self.assertRaises(ValueError):
                    run_episode(model, Mock(), Mock(), 'task', root/f'invalid{i}', budget, resume=resume)
                self.assertFalse(model.requests)

    def test_native_action_still_consumes_decision_budget(self):
        result,_,executor,finalizer=self.episode([Reply('',tool_calls=[call()])],budget=Budget(rounds=1))
        self.assertFalse(result['submitted'])
        self.assertEqual(result['ended_by'],'round_limit')
        executor.execute.assert_called_once()
        finalizer.assert_not_called()

    def test_invalid_arguments_and_extra_resource_controls_rejected(self):
        invalid=[call('submit',{'path':'elsewhere'}),call(args={'code':'pass','memory_mib':9999}),
                 call(args={'code':''}),call('unknown',{}),call(args=['not-object'])]
        invalid.append(dict(call(),arguments='{"code":"pass","code":"other"}'))
        for item in invalid:
            with self.subTest(item=item),self.assertRaises(ValueError):parse_tool_action([item])

    def test_images_are_content_of_correlated_tool_result_and_size_matches(self):
        messages=[{'role':'assistant','text':'','tool_calls':[call()]},
                  {'role':'user','text':'feedback','tool_call_ids':['call_1'],'images':['image.png']}]
        data=b'\x89PNG\r\n\x1a\n123'
        metadata={'model':'test','tools':TOOLS,'instructions':'task'}
        with patch.object(Path,'stat',return_value=SimpleNamespace(st_size=len(data))), \
                patch.object(Path,'read_bytes',return_value=data):
            wire=encode_messages(messages)
            expected=len(json.dumps(dict(metadata,input=wire)).encode())
            self.assertEqual(preflight_request_bytes(metadata,messages,10000),expected)
        self.assertEqual(wire[0]['type'],'function_call')
        self.assertEqual(wire[1]['type'],'function_call_output')
        self.assertEqual(wire[1]['call_id'],'call_1')
        self.assertEqual(wire[1]['output'][1]['type'],'input_image')
        self.assertTrue(wire[1]['output'][1]['image_url'].startswith('data:image/png;base64,'))

    def test_adapter_sends_schemas_and_parses_calls_without_fences(self):
        response={'status':'completed','model':'test','output':[call()],'usage':{}}
        with patch.dict('os.environ',{'OPENAI_API_KEY':'test-only-placeholder'}), \
                patch('benchcad_runtime.openai_api.estimate_request',return_value={}), \
                patch('benchcad_runtime.openai_api.account_response',return_value={}), \
                patch('benchcad_runtime.openai_api.subprocess.run',return_value=SimpleNamespace(
                    returncode=0,stdout=json.dumps(response))) as send:
            reply=OpenAIResponses('test',native_tools=True)('task',[{'role':'user','text':'begin'}],20)
        payload=json.loads(send.call_args.kwargs['input'])['request']
        self.assertEqual(payload['tools'],TOOLS)
        self.assertEqual(payload['tool_choice'],'auto')
        self.assertEqual(reply.tool_calls,[call()])
        self.assertEqual(reply.text,'')

    def test_accounting_includes_definitions_arguments_and_results(self):
        payload={'instructions':'task','model':'test','tools':TOOLS,'input':[
            call(),{'type':'function_call_output','call_id':'call_1','output':[
                {'type':'input_text','text':'feedback'},{'type':'input_image','image_url':'not-counted'}]}]}
        with patch('benchcad_runtime.local_accounting._count',side_effect=len):
            result=estimate_request(payload)
        self.assertEqual(result['image_count'],1)
        self.assertEqual(result['input_tokens'],4+len(json.dumps(TOOLS))+len('execute_python')+
                         len(call()['arguments'])+len('feedback'))

    def test_cli_native_prompt_does_not_request_fences(self):
        prompt=system_prompt('reconstruction',Budget(),native_tools=True)
        self.assertNotIn('```',prompt)
        self.assertIn('execute_python',prompt)
        self.assertIn('submit',prompt)
        self.assertIn('no source STEP',prompt)


if __name__=='__main__':unittest.main()
