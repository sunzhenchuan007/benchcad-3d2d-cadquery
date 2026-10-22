"""Text/image -> Python/submit loop inspired by BenchCAD 2's episode contract.

Backend-neutral loop; transports and executors are supplied by the controller.
One decision response consumes one round; transport failures have separate caps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
import json
import re
import time

from .accounting import summarize_usage
from .journal import Journal
from .errors import SubmissionRejected
from .native_tools import PROTOCOL, GUIDANCE, parse_tool_action
from .local_accounting import (LocalAccountingError, summarize_local_usage,
                               validate_instructions, validate_rates)


class TransportError(RuntimeError):
    """No usable model decision was received; not an execution/code error."""
    def __init__(self, message, local_usage=None, diagnostic=None):
        super().__init__(message)
        self.local_usage = local_usage
        self.diagnostic = diagnostic


@dataclass
class Reply:
    text: str
    usage: dict | None = None
    local_usage: dict | None = None
    executable: bool = True
    response_status: str | None = None
    incomplete_reason: str | None = None
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class Execution:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    images: list[str] = field(default_factory=list)
    timed_out: bool = False


class Executor(Protocol):
    def execute(self, code: str, timeout: float) -> Execution: ...


@dataclass(frozen=True)
class Budget:
    rounds: int = 60
    wall_seconds: float = 7200
    execution_seconds: float = 300
    max_transport_failures: int = 3
    max_consecutive_output_limits: int = 2

    def __post_init__(self):
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not __import__("math").isfinite(v) or v <= 0 for v in vars(self).values()):
            raise ValueError("budget values must be finite and positive")
        if any(type(v) is not int for v in (self.rounds, self.max_transport_failures,
                                           self.max_consecutive_output_limits)):
            raise ValueError("round and retry limits must be integers")


SUBMIT_GUIDANCE = (
    "When the required artifact is saved and ready, submit it with this exact response:\n"
    "```submit\npass\n```\n"
    "The controller then validates the saved artifact; pass does not bypass validation. "
    "A python block, printing 'submitting', or saying 'done' does not submit."
)


def parse_action(text: str) -> tuple[str, str]:
    # Tolerate presentation differences, never silently select/discard code.
    # Multiple Python blocks are one ordered program, one execution, one round.
    blocks = list(re.finditer(
        r"^\s*```[ \t]*(python|py|submit)[ \t]*\r?\n(.*?)^\s*```[ \t]*(?=\r?$)",
        text, re.S | re.M | re.I))
    outside = text
    for block in reversed(blocks):
        outside = outside[:block.start()] + outside[block.end():]
    if (not blocks or "```" in outside
            or any(not b[2].strip() or "```" in b[2] for b in blocks)):
        raise ValueError("Return complete nonempty python blocks, or one submit block; no code executed.")
    kinds = ['submit' if b[1].lower() == 'submit' else 'python' for b in blocks]
    if 'submit' in kinds and len(blocks) != 1:
        raise ValueError("Do not mix submit with other blocks. Send one submit block; no code executed.")
    return kinds[0], '\n\n'.join(b[2].rstrip() for b in blocks)


def clipped(text: str, size: int = 10000) -> str:
    data = text.encode("utf-8")
    if len(data) <= size:
        return text
    return (data[:size // 2].decode("utf-8", "replace") + "\n[output truncated]\n"
            + data[-size // 2:].decode("utf-8", "replace"))


class ReplayModel:
    """Deterministic fixtures only. This is not a model or an AI benchmark run."""
    def __init__(self, replies: list[str]):
        self.replies = iter(replies)

    def __call__(self, system: str, messages: list[dict], timeout: float) -> Reply:
        try:
            return Reply(next(self.replies))
        except StopIteration as exc:
            raise TransportError("Replay exhausted before an explicit submission") from exc


def run_episode(model: Callable, executor: Executor, finalizer: Callable,
                task: str, log_dir: Path, budget: Budget = Budget(),
                clock: Callable = time.monotonic, system_prompt: str | None = None,
                rates: dict | None = None, initial_images: list[str] | None = None,
                resume: dict | None = None) -> dict:
    """A transport must honor timeout; each episode starts with fresh context.

    Failed submit code and explicitly rejected artifacts return feedback within
    the original budget. Unexpected executor/validator failures stop the episode.
    Resume is controller-owned state, never data supplied by the agent.
    """
    if system_prompt is not None:
        validate_instructions(system_prompt)
    validate_rates(rates, (getattr(model, "model", None),))
    native = getattr(model, 'action_protocol', None) == PROTOCOL
    protocol = PROTOCOL if native else 'ordered-python-blocks/2'
    submit_guidance = GUIDANCE if native else SUBMIT_GUIDANCE
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=False)
    previous = resume['episode'] if resume else None
    if previous is not None:
        if previous.get('action_protocol', 'ordered-python-blocks/2') != protocol:
            raise ValueError('Resume cannot change the action protocol')
        revision = resume.get('allow_budget_extension') is True
        if revision:
            old_budget = vars(Budget(**previous['budget']))
            reason = resume.get('reason')
            if (previous['ended_by'] not in ('response_budget_exhausted', 'round_limit')
                    or not isinstance(reason, str) or not reason.strip()
                    or any(getattr(budget, k) < old_budget[k] for k in ('rounds', 'wall_seconds'))
                    or any(getattr(budget, k) != old_budget[k] for k in
                           ('execution_seconds', 'max_transport_failures', 'max_consecutive_output_limits'))):
                raise ValueError('Explicit budget continuation requires a recorded reason and unchanged execution/retry limits')
        elif previous['budget'] != vars(budget) or previous['ended_by'] != 'submit_failed':
            raise ValueError('Resume requires a failed submission with unchanged, unexhausted budgets')
        if (previous['submitted'] or previous['bundle'] is not None
                or not 0 < previous['decision_rounds'] < budget.rounds
                or not 0 <= previous['wall_seconds'] < budget.wall_seconds
                or previous['transport_failures'] >= budget.max_transport_failures):
            raise ValueError('Resume requires an unsubmitted episode with remaining cumulative budgets')
        if not resume.get('messages') or resume.get('feedback', {}).get('role') != 'user':
            raise ValueError('Resume requires the original transcript and pending execution feedback')
    start = clock() - (previous['wall_seconds'] if previous else 0)
    messages = (json.loads(json.dumps(resume['messages'])) if resume else
                [{"role": "user", "text": task, "images": list(initial_images or [])}])
    system = system_prompt if system_prompt is not None else (
        f"Create an engineering drawing from input/model.step. Maximum {budget.rounds} decision rounds. "
        f"Total wall budget: {budget.wall_seconds:g} seconds; each Python execution: "
        f"at most {budget.execution_seconds:g} seconds and the remaining wall budget. "
        "Each reply is exactly one fenced python or submit block, with no other text. "
        "Python executes in a fresh Python process; files persist but Python variables do not. "
        "Feedback contains stdout, stderr and changed PNG images at workspace root or directly in output/. "
        "Read TOOLS.md for available helpers. Save output/drawing.dxf. "
        "A submit block is Python executed once, then the controller validates and freezes the drawing. "
        "Failed submit code or rejected artifacts return feedback for repair within the same budgets. "
        "Only successful validation ends submission. No explicit submit means no answer, even if files exist. "
        "Scores and expert references are never available during this episode."
    )
    if native and system_prompt is None:
        system = (f'Complete the supplied CAD task within {budget.rounds} decisions, '
                  f'{budget.wall_seconds:g} seconds total and {budget.execution_seconds:g} seconds per execution. '
                  'Use the supplied Python libraries and workspace files. Scores and expert answers '
                  'are unavailable. Files persist; Python variables do not. ' + GUIDANCE)
    system += (f"\nAfter {budget.max_consecutive_output_limits} consecutive replies exhausted by "
               "the output-token limit, the controller stops this episode. "
               "The limit includes reasoning; prefer concise, incremental actions.\n" + submit_guidance)
    (log_dir / "system.txt").write_text(system, encoding="utf-8")
    journal = Journal(log_dir / 'journal.jsonl', clock, start)
    journal.write('episode_started', budget=vars(budget), system=system,
                  action_protocol=protocol)
    if resume:
        journal.write('episode_resumed', prior_decision_rounds=previous['decision_rounds'],
                      prior_wall_seconds=previous['wall_seconds'],
                      parent=resume.get('parent'), policy_changed=True,
                      prior_budget=previous['budget'], new_budget=vars(budget),
                      budget_extension=resume.get('allow_budget_extension') is True,
                      reason=resume.get('reason'),
                      max_output_tokens=getattr(model, 'max_output_tokens', None))
    for message in messages:
        journal.write('message', message=message)

    def add_message(message):
        messages.append(message)
        journal.write('message', message=message)

    api_seconds = execution_seconds = finalization_seconds = 0.0
    records, usages, local_usages = [], [], []
    rounds = calls = failures = executions = 0
    if resume:
        api_seconds = previous['timing']['api_seconds']
        execution_seconds = previous['timing']['execution_seconds']
        finalization_seconds = previous['timing']['finalization_seconds']
        records = list(previous['events'])
        usages, local_usages = list(previous['usage_records']), list(previous['local_usage_records'])
        rounds, calls = previous['decision_rounds'], previous['model_call_attempts']
        failures, executions = previous['transport_failures'], previous['code_executions']
        add_message(resume['feedback'])
    ended = "round_limit"
    bundle = None
    consecutive_output_limits = 0
    failure_diagnostic = None
    pending_call_ids = []

    def feedback(body, outcome, images=None):
        """Every actionable observation carries the same remaining-budget state."""
        remaining_rounds = budget.rounds - rounds
        remaining_seconds = max(0, budget.wall_seconds - (clock() - start))
        state = {'outcome': outcome, 'round': rounds,
                 'rounds_remaining': remaining_rounds,
                 'seconds_remaining': round(remaining_seconds, 1),
                 'artifact_accepted': False}
        header = (f"Round {rounds}/{budget.rounds}; remaining {remaining_rounds}. "
                  f"Remaining seconds: {remaining_seconds:.1f}.\n"
                  f"Outcome: {outcome}. No submission accepted yet.\n")
        reminder = ('\nBudget nearly exhausted: reserve a decision for explicit submit if ready.\n'
                    + submit_guidance if 0 < remaining_rounds <= 3 else '')
        message = {'role': 'user', 'text': header + body + reminder,
                   'images': list(images or []), 'feedback': state}
        if pending_call_ids:
            message['tool_call_ids'] = list(pending_call_ids)
        add_message(message)
        pending_call_ids.clear()

    while rounds < budget.rounds:
        left = budget.wall_seconds - (clock() - start)
        if left <= 0:
            ended = "wall_timeout"
            break
        calls += 1
        call_start = clock()
        journal.write('request_started', call=calls, next_round=rounds + 1,
                      timeout_seconds=left, message_count=len(messages),
                      image_count=sum(len(m.get('images', [])) for m in messages),
                      text_characters=sum(len(m.get('text', '')) for m in messages))
        try:
            reply = model(system, messages, timeout=left)
        except TransportError as exc:
            duration = clock() - call_start
            api_seconds += duration
            journal.write('request_failed', call=calls, duration_seconds=duration,
                          error_type=type(exc).__name__, error=str(exc),
                          local_usage=exc.local_usage, diagnostic=exc.diagnostic)
            usages.append(None)
            local_usages.append(exc.local_usage)
            failures += 1
            records.append({"event": "transport_failure", "call": calls, "error": str(exc),
                            "diagnostic": exc.diagnostic})
            if failures >= budget.max_transport_failures:
                ended = "infrastructure_failed"
                break
            continue
        except Exception as exc:
            duration = clock() - call_start
            api_seconds += duration
            journal.write('request_failed', call=calls, duration_seconds=duration,
                          error_type=type(exc).__name__,
                          local_usage=getattr(exc, 'local_usage', None),
                          diagnostic=getattr(exc, 'diagnostic', None))
            usages.append(None)
            local_usages.append(getattr(exc, "local_usage", None))
            event = {"event": "model_client_failed", "call": calls, "error_type": type(exc).__name__,
                     "diagnostic": getattr(exc, 'diagnostic', None)}
            if isinstance(exc, LocalAccountingError):
                # Our accounting errors contain actionable, generic diagnostics only.
                event["error"] = str(exc)
            records.append(event)
            ended = "infrastructure_failed"
            break
        rounds += 1
        duration = clock() - call_start
        api_seconds += duration
        journal.write('response_received', call=calls, round=rounds,
                      duration_seconds=duration, status=reply.response_status,
                      incomplete_reason=reply.incomplete_reason,
                      local_usage=reply.local_usage, provider_usage=reply.usage)
        usages.append(reply.usage)
        local_usages.append(reply.local_usage)
        (log_dir / f"reply_{rounds:02d}.txt").write_text(reply.text, encoding="utf-8")
        assistant = {"role": "assistant", "text": reply.text, "images": []}
        if native:
            assistant['tool_calls'] = reply.tool_calls
            pending_call_ids = [call['call_id'] for call in reply.tool_calls]
        add_message(assistant)
        left = budget.wall_seconds - (clock() - start)
        if left <= 0:
            ended = "wall_timeout"
            break
        if not reply.executable:
            consecutive_output_limits = (consecutive_output_limits + 1
                                         if reply.incomplete_reason == 'max_output_tokens' else 0)
            journal.write('unexecutable_response', round=rounds, status=reply.response_status,
                          incomplete_reason=reply.incomplete_reason)
            records.append({"round": rounds, "event": "unexecutable_response",
                            "response_status": reply.response_status,
                            "incomplete_reason": reply.incomplete_reason})
            reason = ('Response reached the configured output-token limit (including reasoning); '
                      if reply.incomplete_reason == 'max_output_tokens' else
                      'Response was incomplete or refused; ')
            stop = consecutive_output_limits >= budget.max_consecutive_output_limits
            guidance = ("Episode stopped after repeated output-token exhaustion; no further request will be sent."
                        if stop else "Return a complete, concise action; do not assume the previous code ran.")
            feedback(reason + "no code executed. " + guidance,
                     'response_not_executed')
            if stop:
                ended = 'response_budget_exhausted'
                failure_diagnostic = {
                    'failure_kind': 'consecutive_output_token_limits',
                    'attribution': 'response_budget',
                    'consecutive_count': consecutive_output_limits,
                    'limit': budget.max_consecutive_output_limits,
                }
                records.append({'event': ended, 'round': rounds, **failure_diagnostic})
                journal.write(ended, round=rounds, **failure_diagnostic)
                break
            continue
        consecutive_output_limits = 0
        try:
            action, code = parse_tool_action(reply.tool_calls) if native else parse_action(reply.text)
        except ValueError as exc:
            journal.write('format_error', round=rounds)
            records.append({"round": rounds, "event": "format_error"})
            repair = GUIDANCE if native else (
                     'Use this format for your next action, replacing the example with your code:\n'
                     '```python\nprint("ready")\n```\n'
                     'No XML/tool-call markup: this interface executes fenced Python only.\n'
                     + SUBMIT_GUIDANCE)
            feedback(str(exc) + ' No code executed; workspace files were not changed by this response.\n'
                     + repair, 'format_error')
            continue
        execution_start = clock()
        journal.write('action_parsed', round=rounds, action=action, code=code,
                      protocol=protocol)
        executes = not (native and action == 'submit')
        journal.write('execution_started' if executes else 'submission_requested', round=rounds, action=action)
        try:
            result = executor.execute(code, min(budget.execution_seconds, left)) if executes else Execution(0)
        except Exception as exc:
            duration = clock() - execution_start
            execution_seconds += duration
            journal.write('execution_failed', round=rounds, duration_seconds=duration,
                          error_type=type(exc).__name__)
            records.append({"round": rounds, "event": "executor_failed", "error": str(exc)})
            ended = "infrastructure_failed"
            break
        executions += int(executes)
        duration = clock() - execution_start
        execution_seconds += duration
        if executes:
            journal.write('execution_finished', round=rounds, duration_seconds=duration,
                          action=action, exit_code=result.exit_code, timed_out=result.timed_out,
                          stdout=result.stdout, stderr=result.stderr, images=result.images)
        records.append({"round": rounds, "event": action, "exit_code": result.exit_code,
                        "timed_out": result.timed_out, "image_count": len(result.images)})
        if action == "submit":
            rejection = 'Submission code failed; no artifact was accepted.'
            if result.exit_code == 0:
                left = budget.wall_seconds - (clock() - start)
                if left <= 0:
                    ended = "wall_timeout"
                else:
                    finalization_start = clock()
                    journal.write('finalization_started', round=rounds)
                    try:
                        bundle = finalizer(timeout=left)
                        ended = "submitted" if clock()-start <= budget.wall_seconds else "wall_timeout"
                    except SubmissionRejected as exc:
                        rejection = str(exc)
                        records.append({"round": rounds, "event": "submission_rejected", "error": rejection})
                    except TimeoutError:
                        ended = "wall_timeout"
                    except Exception as exc:
                        ended = "infrastructure_failed"
                        records.append({"round": rounds, "event": "finalization_failed",
                                        "error_type": type(exc).__name__})
                    finally:
                        duration = clock() - finalization_start
                        finalization_seconds += duration
                        journal.write('finalization_finished', round=rounds,
                                      duration_seconds=duration, ended_by=ended)
            if ended in ('submitted', 'wall_timeout', 'infrastructure_failed'):
                if native and ended == 'submitted':
                    add_message({'role':'user', 'text':'Submission accepted and frozen. Episode ended.',
                                 'images':[], 'tool_call_ids':list(pending_call_ids)})
                    pending_call_ids.clear()
                break
            journal.write('submission_rejected', round=rounds, error=rejection)
            result.stderr += '\nSubmission not accepted: ' + rejection + (
                '\nYou may repair and submit again within the remaining rounds/time; budgets do not reset.')
        text = (f"Exit code {result.exit_code}; timed_out={result.timed_out}\n"
                f"stdout:\n{clipped(result.stdout)}\nstderr:\n{clipped(result.stderr)}")
        outcome = ('submission_rejected' if action == 'submit' else
                   'execution_timeout' if result.timed_out else
                   'execution_failed' if result.exit_code else 'execution_succeeded')
        if result.exit_code or result.timed_out:
            text += '\nFiles written before the failure may persist; execution is not rolled back.'
        else:
            text += '\nExecution success is not a check of task correctness.'
        if '[output truncated]' in text:
            text += ('\nOutput was truncated. Query the relevant subset or save verbose data '
                     'to a workspace file and read selected portions in later rounds.')
        feedback(text, outcome, result.images)
    local_summary = summarize_local_usage(local_usages, rates)
    provider_summary = summarize_usage(usages)
    result = {"schema": "benchcad-episode/1", "ended_by": ended,
              "failure_diagnostic": failure_diagnostic,
              "action_protocol": protocol,
              "submitted": ended == "submitted", "decision_rounds": rounds,
              "model_call_attempts": calls, "transport_failures": failures,
              "code_executions": executions, "wall_seconds": clock() - start,
              "timing": {"api_seconds": api_seconds, "execution_seconds": execution_seconds,
                         "finalization_seconds": finalization_seconds},
              "budget": vars(budget),
              "usage_metric": "local_visible_text",
              "local_usage_records": local_usages, "local_visible_text": local_summary,
              "provider_usage": provider_summary,
              "usage_records": usages, "usage_complete": provider_summary["usage_complete"],
              "bundle": bundle, "events": records,
              "formal_score": False, "actual_fee": None}
    if resume:
        result['continuation'] = {'parent': resume.get('parent'), 'policy_changed': True,
                                  'prior_decision_rounds': previous['decision_rounds'],
                                  'prior_wall_seconds': previous['wall_seconds'],
                                  'paused_time_excluded': True}
    (log_dir / "episode.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (log_dir / "transcript.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    journal.write('episode_finished', ended_by=ended, decision_rounds=rounds,
                  model_call_attempts=calls, code_executions=executions)
    return result
