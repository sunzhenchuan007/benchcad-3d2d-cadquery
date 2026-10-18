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
        result, executor = self.run_case(["```python\nprint(1)\n```"]*31)
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
        self.assertLessEqual(seen[0][1], 1070)
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
