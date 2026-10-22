import json
from pathlib import Path
import tempfile
import unittest

from benchcad_runtime.errors import SubmissionRejected
from benchcad_runtime.episode import Budget, Execution, ReplayModel, Reply, TransportError, run_episode, parse_action


class Executor:
    def __init__(self, code=0):
        self.calls = []
        self.code = code
    def execute(self, source, timeout):
        self.calls.append(source)
        return Execution(self.code, stdout="real feedback", images=["snapshot.png"])


class EpisodeTests(unittest.TestCase):
    def run_case(self, replies, executor=None, budget=None, finalizer=None):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        executor = executor or Executor()
        finalizer = finalizer or (lambda **kw: "frozen")
        result = run_episode(ReplayModel(replies), executor, finalizer, "task", Path(self.temp.name)/"logs", budget or Budget())
        return result, executor

    def test_success_exec_then_submit(self):
        result, executor = self.run_case(["```python\nprint(1)\n```", "```submit\nprint(2)\n```"])
        self.assertEqual(result["ended_by"], "submitted")
        self.assertEqual(result["decision_rounds"], 2)
        self.assertEqual(len(executor.calls), 2)
        self.assertFalse(result["usage_complete"])
        transcript = json.loads((Path(self.temp.name)/"logs/transcript.json").read_text())
        self.assertEqual(transcript[2]["images"], ["snapshot.png"])
        self.assertIn("real feedback", transcript[2]["text"])

    def test_30_round_cap_no_auto_submission(self):
        result, executor = self.run_case(["```python\nprint(1)\n```"]*31, budget=Budget(rounds=30))
        self.assertEqual(result["decision_rounds"], 30)
        self.assertEqual(len(executor.calls), 30)
        self.assertEqual(result["ended_by"], "round_limit")
        self.assertFalse(result["submitted"])
        self.assertIsNone(result["bundle"])

    def test_format_error_consumes_round_without_execution(self):
        result, executor = self.run_case(["I am finished"], budget=Budget(rounds=1))
        self.assertEqual(result["decision_rounds"], 1)
        self.assertFalse(executor.calls)
        self.assertFalse(result["submitted"])

    def test_multiple_blocks_are_not_silently_selected(self):
        with self.assertRaises(ValueError):
            parse_action("```python\npass\n```\n```submit\npass\n```")

    def test_python_blocks_preserve_order_and_share_one_round(self):
        response = 'Plan:\n```Python\nx = 3\n```\nThen:\n```py\nprint(x)\n```'
        action, source = parse_action(response)
        self.assertEqual(action, 'python')
        self.assertEqual(source, 'x = 3\n\nprint(x)')
        result, executor = self.run_case([response, '```submit\npass\n```'])
        self.assertEqual(result['decision_rounds'], 2)
        self.assertEqual(result['code_executions'], 2)
        self.assertEqual(executor.calls[0], source)
        self.assertEqual(result['action_protocol'], 'ordered-python-blocks/2')

    def test_ambiguous_or_incomplete_blocks_never_execute(self):
        cases = ['```python\nprint(1)', '```submit\npass\n```\n```submit\npass\n```',
                 '```python\npass\n```\n```bash\necho hi\n```', '```python\n\n```']
        for response in cases:
            with self.subTest(response=response), self.assertRaises(ValueError):
                parse_action(response)

    def test_single_block_works_with_prose_crlf_and_case(self):
        self.assertEqual(parse_action('Here:\r\n```SUBMIT\r\npass\r\n```\r\nDone.'),
                         ('submit', 'pass'))

    def test_feedback_tracks_errors_success_and_history_without_reset(self):
        class PartialFailure(Executor):
            def execute(self, code, timeout):
                self.calls.append(code)
                if len(self.calls) == 1:
                    return Execution(1, stdout='x'*12000, stderr='specific repair error')
                return Execution(0)
        replies = ['not code', '```python\npass\n```', '```python\npass\n```',
                   '```submit\npass\n```']
        result, _ = self.run_case(replies, PartialFailure(), Budget(rounds=4))
        log = Path(self.temp.name)/'logs'
        messages = json.loads((log/'transcript.json').read_text())
        feedback = [m for m in messages if 'feedback' in m]
        self.assertEqual([m['feedback']['outcome'] for m in feedback],
                         ['format_error', 'execution_failed', 'execution_succeeded'])
        self.assertEqual([m['feedback']['rounds_remaining'] for m in feedback], [3,2,1])
        self.assertTrue(all(not m['feedback']['artifact_accepted'] for m in feedback))
        self.assertIn('No code executed', feedback[0]['text'])
        self.assertIn('```python\nprint("ready")\n```', feedback[0]['text'])
        self.assertIn('```submit\npass\n```', feedback[0]['text'])
        self.assertIn('specific repair error', feedback[1]['text'])
        self.assertIn('not rolled back', feedback[1]['text'])
        self.assertIn('Output was truncated', feedback[1]['text'])
        self.assertIn('not a check of task correctness', feedback[2]['text'])
        journal = [json.loads(s) for s in (log/'journal.jsonl').read_text().splitlines()]
        requests = [r for r in journal if r['event']=='request_started']
        self.assertEqual([r['message_count'] for r in requests], [1,3,5,7])
        self.assertTrue(result['submitted'])

    def test_incomplete_reply_never_executes_and_returns_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            def model(system, messages, timeout):
                seen.append(list(messages))
                if len(seen) == 1:
                    return Reply('```submit\npass\n```', executable=False, response_status='incomplete',
                                 incomplete_reason='max_output_tokens')
                self.assertEqual(messages[-1]['feedback']['outcome'], 'response_not_executed')
                self.assertIn('Remaining seconds:', messages[-1]['text'])
                self.assertIn('output-token limit', messages[-1]['text'])
                return Reply('```submit\npass\n```')
            executor = Executor()
            result = run_episode(model, executor, lambda **kw:'frozen', 'task', Path(tmp)/'logs')
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(result['decision_rounds'], 2)

    def test_repeated_output_limits_stop_without_execution_or_auto_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def model(system, messages, timeout):
                calls.append(1)
                self.assertIn('2 consecutive', system)
                return Reply('```submit\npass\n```', executable=False,
                             response_status='incomplete', incomplete_reason='max_output_tokens',
                             usage={'input_tokens': 10, 'output_tokens': 8192})
            executor = Executor()
            def forbidden_finalizer(**kwargs):
                self.fail('Incomplete response must not finalize')
            result = run_episode(model, executor, forbidden_finalizer, 'task', Path(tmp)/'logs')
            messages = json.loads((Path(tmp)/'logs/transcript.json').read_text())
            self.assertEqual(len(calls), 2)
            self.assertEqual(result['decision_rounds'], 2)
            self.assertEqual(result['ended_by'], 'response_budget_exhausted')
            self.assertEqual(result['failure_diagnostic']['attribution'], 'response_budget')
            self.assertEqual(len(result['usage_records']), 2)
            self.assertFalse(executor.calls)
            self.assertFalse(result['submitted'])
            self.assertIsNone(result['bundle'])
            self.assertEqual(messages[-1]['feedback']['rounds_remaining'], 58)
            self.assertIn('no further request', messages[-1]['text'])

    def test_output_limit_streak_resets_after_other_response(self):
        for middle in (Reply('not fenced'), Reply('', executable=False, incomplete_reason='content_filter'),
                       Reply('```python\npass\n```')):
            with self.subTest(middle=middle), tempfile.TemporaryDirectory() as tmp:
                limited = Reply('', executable=False, incomplete_reason='max_output_tokens')
                replies = iter([limited, middle, limited, Reply('```submit\npass\n```')])
                result = run_episode(lambda *a, **kw: next(replies), Executor(), lambda **kw:'frozen',
                                     'task', Path(tmp)/'logs')
                self.assertTrue(result['submitted'])
                self.assertEqual(result['decision_rounds'], 4)
                self.assertIsNone(result['failure_diagnostic'])

    def test_output_limit_stop_is_configurable_and_validated(self):
        for invalid in (0, -1, True, 1.5, float('inf')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Budget(max_consecutive_output_limits=invalid)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_episode(lambda *a, **kw: Reply('', executable=False,
                                 incomplete_reason='max_output_tokens'), Executor(), lambda **kw:'frozen',
                                 'task', Path(tmp)/'logs', Budget(max_consecutive_output_limits=3))
            self.assertEqual(result['decision_rounds'], 3)
            self.assertEqual(result['failure_diagnostic']['limit'], 3)

    def test_printing_submitting_requires_explicit_submit_and_shows_example(self):
        replies = ['```python\nprint("submitting")\n```', '```submit\npass\n```']
        finalized = []
        result, executor = self.run_case(replies, budget=Budget(rounds=2),
                                        finalizer=lambda **kw: finalized.append(1) or 'frozen')
        messages = json.loads((Path(self.temp.name)/'logs/transcript.json').read_text())
        self.assertEqual([e['event'] for e in result['events']], ['python', 'submit'])
        self.assertFalse(messages[2]['feedback']['artifact_accepted'])
        self.assertEqual(messages[2]['feedback']['rounds_remaining'], 1)
        self.assertIn('```submit\npass\n```', messages[2]['text'])
        self.assertIn('does not submit', messages[2]['text'])
        self.assertTrue(result['submitted'])
        self.assertEqual(finalized, [1])
        self.assertEqual(len(executor.calls), 2)

    def test_submit_failure_returns_feedback_and_allows_repair(self):
        class Recovering(Executor):
            def execute(self, source, timeout):
                self.calls.append(source)
                return Execution(1 if len(self.calls) == 1 else 0, stderr="assertion failed")
        result, executor = self.run_case(["```submit\nraise Exception()\n```", "```submit\npass\n```"], Recovering())
        self.assertEqual(result["ended_by"], "submitted")
        self.assertEqual(len(executor.calls), 2)
        transcript = json.loads((Path(self.temp.name)/"logs/transcript.json").read_text())
        self.assertIn('assertion failed', transcript[2]['text'])
        self.assertIn('Submission not accepted', transcript[2]['text'])

    def test_last_round_failed_submit_has_no_fallback(self):
        result, executor = self.run_case(["```submit\npass\n```"], Executor(1), Budget(rounds=1))
        self.assertEqual(result['ended_by'], 'round_limit')
        self.assertEqual(len(executor.calls), 1)
        self.assertIsNone(result['bundle'])

    def test_known_validation_rejection_can_be_repaired(self):
        attempts = []
        def finalize(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise SubmissionRejected('empty DXF')
            return 'frozen'
        result, _ = self.run_case(["```submit\npass\n```"]*2, finalizer=finalize)
        self.assertTrue(result['submitted'])
        self.assertEqual(result['decision_rounds'], 2)
        self.assertEqual(result['events'][1]['event'], 'submission_rejected')

    def test_unknown_validator_failure_is_infrastructure(self):
        def fail(**kwargs):
            raise RuntimeError("renderer unavailable")
        result, _ = self.run_case(["```submit\npass\n```"], finalizer=fail)
        self.assertEqual(result["ended_by"], "infrastructure_failed")
        self.assertIsNone(result["bundle"])

    def test_transport_failure_does_not_consume_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = [0]
            def model(*args, **kwargs):
                n[0] += 1
                if n[0] < 3:
                    raise TransportError("network")
                return Reply("```submit\npass\n```", {"output_tokens": 4})
            result = run_episode(model, Executor(), lambda **kw: "frozen", "task", Path(tmp)/"logs")
            self.assertEqual((result["decision_rounds"],result["model_call_attempts"]), (1,3))

    def test_transport_failures_are_bounded(self):
        result, executor = self.run_case([])
        self.assertEqual(result["ended_by"], "infrastructure_failed")
        self.assertEqual(result["transport_failures"], 3)
        self.assertEqual(result["decision_rounds"], 0)

    def test_overdue_model_reply_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0]
            def model(*args, **kwargs):
                now[0] = 31
                return Reply("```submit\npass\n```")
            executor = Executor()
            result = run_episode(model, executor, lambda **kw: "frozen", "task", Path(tmp)/"logs",
                                 Budget(wall_seconds=30), clock=lambda: now[0])
            self.assertEqual(result["ended_by"], "wall_timeout")
            self.assertEqual(len(executor.calls), 0)

    def test_execution_failure_is_returned_to_next_decision(self):
        result, executor = self.run_case(["```python\nraise Exception()\n```", "```submit\npass\n```"], Executor(1), Budget(rounds=2))
        self.assertEqual(result["decision_rounds"], 2)
        self.assertEqual(len(executor.calls), 2)

    def test_executor_launch_failure_is_not_model_code_failure(self):
        class BrokenExecutor:
            def execute(self, *args):
                raise OSError("CAD process could not start")
        result, _ = self.run_case(["```python\npass\n```"], BrokenExecutor())
        self.assertEqual(result["ended_by"], "infrastructure_failed")
        self.assertEqual(result["code_executions"], 0)
        self.assertEqual(result["decision_rounds"], 1)
        self.assertEqual(result["events"][0]["event"], "executor_failed")

    def test_resume_preserves_context_and_cumulative_budgets(self):
        first, _ = self.run_case(["```submit\npass\n```"], Executor(1), Budget(rounds=1))
        first.update(ended_by='submit_failed', decision_rounds=27, code_executions=26,
                     model_call_attempts=27, wall_seconds=730, budget=vars(Budget()))
        transcript = json.loads((Path(self.temp.name)/"logs/transcript.json").read_text())
        seen = []
        def model(system, messages, timeout):
            seen.append((messages[:], timeout))
            return Reply("```submit\npass\n```")
        resumed = run_episode(model, Executor(), lambda **kw: 'frozen', 'unused',
            Path(self.temp.name)/'continued', resume={'episode': first, 'messages': transcript,
            'feedback': {'role': 'user', 'text': 'original submission error', 'images': []}, 'parent': 'prior'})
        self.assertTrue(resumed['submitted'])
        self.assertEqual(resumed['decision_rounds'], 28)
        self.assertEqual(resumed['code_executions'], 27)
        self.assertEqual(resumed['model_call_attempts'], 28)
        self.assertLessEqual(seen[0][1], Budget().wall_seconds - 730)
        self.assertGreaterEqual(resumed['wall_seconds'], 730)
        self.assertEqual(seen[0][0][-1]['text'], 'original submission error')
        self.assertEqual(seen[0][0][0], transcript[0])
        self.assertEqual(len(resumed['usage_records']), len(first['usage_records'])+1)

    def test_resume_cannot_reset_budget(self):
        first, _ = self.run_case(["```submit\npass\n```"], Executor(1), Budget(rounds=1))
        first['ended_by'] = 'submit_failed'
        with self.assertRaises(ValueError):
            run_episode(ReplayModel([]), Executor(), lambda **kw: 'frozen', 'task',
                Path(self.temp.name)/'invalid', resume={'episode': first, 'messages': [], 'feedback': {}})

    def test_failed_submit_at_deadline_does_not_request_more_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0]
            class TimedExecutor:
                def execute(self, code, timeout):
                    now[0] = 30
                    return Execution(124, timed_out=True)
            result = run_episode(ReplayModel(["```submit\npass\n```"]), TimedExecutor(),
                lambda **kw: 'frozen', 'task', Path(tmp)/'logs', Budget(wall_seconds=30), clock=lambda: now[0])
            self.assertEqual(result['ended_by'], 'wall_timeout')
            self.assertEqual(result['model_call_attempts'], 1)


if __name__ == "__main__":
    unittest.main()
