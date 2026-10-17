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
    rounds: int = 30
    wall_seconds: float = 1800
    execution_seconds: float = 300
    max_transport_failures: int = 3

    def __post_init__(self):
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not __import__("math").isfinite(v) or v <= 0 for v in vars(self).values()):
            raise ValueError("budget values must be finite and positive")
        if type(self.rounds) is not int or type(self.max_transport_failures) is not int:
            raise ValueError("round and retry limits must be integers")


def parse_action(text: str) -> tuple[str, str]:
    # Strict public protocol. Never silently choose one of several code blocks.
    match = re.fullmatch(r"\s*```(python|submit)\s*\n(.*?)\n```\s*", text, re.S)
    if not match or not match[2].strip() or "```" in match[2]:
        raise ValueError("Return exactly one nonempty ```python or ```submit block.")
    return match[1], match[2]


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
                rates: dict | None = None, initial_images: list[str] | None = None) -> dict:
    """A transport must honor timeout; each episode starts with fresh context.

    Final submit code consumes a decision round and executes once. A failed
    submit or a failed finalizer ends this episode with no model repair loop.
    """
    if system_prompt is not None:
        validate_instructions(system_prompt)
    validate_rates(rates, (getattr(model, "model", None),))
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=False)
    start = clock()
    messages = [{"role": "user", "text": task, "images": list(initial_images or [])}]
    system = system_prompt if system_prompt is not None else (
        f"Create an engineering drawing from input/model.step. Maximum {budget.rounds} decision rounds. "
        f"Total wall budget: {budget.wall_seconds:g} seconds; each Python execution: "
        f"at most {budget.execution_seconds:g} seconds and the remaining wall budget. "
        "Each reply is exactly one fenced python or submit block, with no other text. "
        "Python executes in a fresh Python process; files persist but Python variables do not. "
        "Feedback contains stdout, stderr and changed PNG images at workspace root or directly in output/. "
        "Read TOOLS.md for available helpers. Save output/drawing.dxf. "
        "A submit block is Python executed once, then the controller validates and freezes the drawing. "
        "Submit failure ends the task. No explicit submit means no answer, even if files exist. "
        "Scores and expert references are never available during this episode."
    )
    (log_dir / "system.txt").write_text(system, encoding="utf-8")
    journal = Journal(log_dir / 'journal.jsonl', clock, start)
    journal.write('episode_started', budget=vars(budget), system=system)
    journal.write('message', message=messages[0])

    def add_message(message):
        messages.append(message)
        journal.write('message', message=message)

    api_seconds = execution_seconds = finalization_seconds = 0.0
    records, usages, local_usages = [], [], []
    rounds = calls = failures = executions = 0
    ended = "round_limit"
    bundle = None
    while rounds < budget.rounds:
        left = budget.wall_seconds - (clock() - start)
        if left <= 0:
            ended = "wall_timeout"
            break
        calls += 1
        call_start = clock()
        journal.write('request_started', call=calls, next_round=rounds + 1,
                      timeout_seconds=left)
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
                      local_usage=reply.local_usage, provider_usage=reply.usage)
        usages.append(reply.usage)
        local_usages.append(reply.local_usage)
        (log_dir / f"reply_{rounds:02d}.txt").write_text(reply.text, encoding="utf-8")
        add_message({"role": "assistant", "text": reply.text, "images": []})
        left = budget.wall_seconds - (clock() - start)
        if left <= 0:
            ended = "wall_timeout"
            break
        if not reply.executable:
            journal.write('unexecutable_response', round=rounds, status=reply.response_status)
            records.append({"round": rounds, "event": "unexecutable_response",
                            "response_status": reply.response_status})
            add_message({"role": "user", "text": "Response was incomplete or refused; no code executed. "
                             f"Return one complete action. Remaining rounds: {budget.rounds-rounds}.", "images": []})
            continue
        try:
            action, code = parse_action(reply.text)
        except ValueError as exc:
            journal.write('format_error', round=rounds)
            records.append({"round": rounds, "event": "format_error"})
            add_message({"role": "user", "text": f"{exc} Remaining rounds: {budget.rounds-rounds}.", "images": []})
            continue
        execution_start = clock()
        journal.write('execution_started', round=rounds, action=action)
        try:
            result = executor.execute(code, min(budget.execution_seconds, left))
        except Exception as exc:
            duration = clock() - execution_start
            execution_seconds += duration
            journal.write('execution_failed', round=rounds, duration_seconds=duration,
                          error_type=type(exc).__name__)
            records.append({"round": rounds, "event": "executor_failed", "error": str(exc)})
            ended = "infrastructure_failed"
            break
        executions += 1
        duration = clock() - execution_start
        execution_seconds += duration
        journal.write('execution_finished', round=rounds, duration_seconds=duration,
                      action=action, exit_code=result.exit_code, timed_out=result.timed_out,
                      stdout=result.stdout, stderr=result.stderr, images=result.images)
        records.append({"round": rounds, "event": action, "exit_code": result.exit_code,
                        "timed_out": result.timed_out, "image_count": len(result.images)})
        if action == "submit":
            ended = "submit_failed"
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
                    except Exception as exc:
                        records.append({"round": rounds, "event": "finalization_failed", "error": str(exc)})
                    finally:
                        duration = clock() - finalization_start
                        finalization_seconds += duration
                        journal.write('finalization_finished', round=rounds,
                                      duration_seconds=duration, ended_by=ended)
            break
        text = (f"Round {rounds}/{budget.rounds}; remaining {budget.rounds-rounds}. "
                f"Remaining seconds: {max(0, budget.wall_seconds - (clock()-start)):.1f}. "
                f"Exit code {result.exit_code}; timed_out={result.timed_out}\n"
                f"stdout:\n{clipped(result.stdout)}\nstderr:\n{clipped(result.stderr)}")
        add_message({"role": "user", "text": text, "images": result.images})
    local_summary = summarize_local_usage(local_usages, rates)
    provider_summary = summarize_usage(usages)
    result = {"schema": "benchcad-episode/1", "ended_by": ended,
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
    (log_dir / "episode.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (log_dir / "transcript.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    journal.write('episode_finished', ended_by=ended, decision_rounds=rounds,
                  model_call_attempts=calls, code_executions=executions)
    return result
