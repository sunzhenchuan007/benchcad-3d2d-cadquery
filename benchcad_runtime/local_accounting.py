"""Offline, visible-text-only accounting. Never an API usage or billing oracle."""
from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
import importlib.metadata
import math
import json
import os
from pathlib import Path
import tempfile

ENCODING = "o200k_base"
TIKTOKEN_VERSION = "0.12.0"
_VOCAB_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
_VOCAB_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
# Pinned tiktoken 0.12.0 o200k_base definition. Loading directly avoids the
# upstream loader's implicit downloads and cache deletion on a hash mismatch.
_PATTERN = "|".join([
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r"\p{N}{1,3}", r" ?[^\s\p{L}\p{N}]+[\r\n/]*", r"\s*[\r\n]+",
    r"\s+(?!\S)", r"\s+",
])


class LocalAccountingError(RuntimeError):
    """Required local tokenizer or payload cannot be accounted for safely."""


def validate_instructions(instructions):
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("System instructions must be nonempty, non-whitespace text")


@lru_cache(maxsize=1)
def _load_encoding():
    try:
        import tiktoken
        version = importlib.metadata.version("tiktoken")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise LocalAccountingError("Local accounting requires tiktoken==0.12.0 (local-accounting extra)") from exc
    if version != TIKTOKEN_VERSION:
        raise LocalAccountingError("Local accounting requires exactly tiktoken==0.12.0")
    cache_dir = os.environ.get("TIKTOKEN_CACHE_DIR", os.environ.get(
        "DATA_GYM_CACHE_DIR", str(Path(tempfile.gettempdir()) / "data-gym-cache")))
    if not cache_dir:
        raise LocalAccountingError("Offline o200k_base cache is disabled; downloads are not allowed")
    cache_file = Path(cache_dir) / hashlib.sha1(_VOCAB_URL.encode()).hexdigest()
    try:
        data = cache_file.read_bytes()
    except OSError as exc:
        raise LocalAccountingError("Offline o200k_base cache is missing or unreadable; no download attempted") from exc
    if hashlib.sha256(data).hexdigest() != _VOCAB_SHA256:
        raise LocalAccountingError("Offline o200k_base cache checksum mismatch; no download attempted")
    try:
        ranks = {base64.b64decode(token, validate=True): int(rank)
                 for token, rank in (line.split() for line in data.splitlines() if line)}
        return tiktoken.Encoding(name=ENCODING, pat_str=_PATTERN,
                                 mergeable_ranks=ranks,
                                 special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018})
    except Exception as exc:
        raise LocalAccountingError("Offline o200k_base cache could not be loaded") from exc


def _count(text):
    if not isinstance(text, str):
        raise LocalAccountingError("Visible text parts must be strings")
    # Special-token-looking user text is ordinary text, never a control token.
    return len(_load_encoding().encode_ordinary(text))


def estimate_request(payload):
    """Count the actual outgoing instructions and every history text part anew."""
    validate_instructions(payload.get("instructions"))
    parts = [_count(payload["instructions"])]
    if payload.get('tools'):
        parts.append(_count(json.dumps(payload['tools'])))
    images = 0
    for message in payload["input"]:
        if message.get('type') == 'function_call':
            parts.extend([_count(message['name']), _count(message['arguments'])])
            continue
        content = message['output'] if message.get('type') == 'function_call_output' else message['content']
        if isinstance(content, str):
            parts.append(_count(content))
        else:
            for part in content:
                if part["type"] in ("input_text", "output_text"):
                    parts.append(_count(part["text"]))
                elif part["type"] == "input_image":
                    images += 1
                else:
                    raise LocalAccountingError("Unsupported outgoing content part for local accounting")
    return {
        "metric": "local_visible_text", "model": payload.get("model"),
        "response_model": None, "encoding": ENCODING, "tiktoken_version": TIKTOKEN_VERSION,
        "model_encoding_verified": False, "encoding_role": "unverified_model_proxy",
        "method": "sum_separately_encoded_text_parts",
        "input_part_tokens": parts, "input_tokens": sum(parts),
        "output_part_tokens": None, "output_tokens": None,
        "image_count": images, "image_tokens": None,
        "response_received": False, "usage_complete": False,
    }


def account_response(request_usage, text_parts, response_model=None):
    """Count visible parts even when the response cannot be executed."""
    counts = [_count(part) for part in text_parts]
    return {**request_usage, "response_model": response_model,
            "output_part_tokens": counts, "output_tokens": sum(counts),
            "response_received": True, "usage_complete": True}


def validate_rates(rates, models=()):
    if rates is None:
        return
    for key in ("input_per_million", "output_per_million"):
        value = rates.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Visible-text rates must be finite nonnegative numbers")
    # Cached rates do not affect this metric, but reject invalid supplied data.
    if "cached_input_per_million" in rates:
        value = rates["cached_input_per_million"]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Cached rate must be a finite nonnegative number")
    for key in ("model", "date", "source_url"):
        if not isinstance(rates.get(key), str) or not rates[key].strip():
            raise ValueError("Pricing requires model, date and source_url metadata")
    if any(model != rates["model"] for model in models if model is not None):
        raise ValueError("Pricing model must match known request and response model IDs")


def summarize_local_usage(records, rates=None):
    """Known subtotals are partial evidence; price only a complete text ledger."""
    records = list(records)
    known = [record for record in records if record is not None]
    models = {r.get(key) for r in known for key in ("model", "response_model")
              if r.get(key) is not None}
    validate_rates(rates, models)
    totals = {"input_tokens": 0, "output_tokens": 0, "image_count": 0}
    observed = {key: False for key in totals}
    complete = bool(records) and len(known) == len(records)
    metadata = []
    for record in known:
        descriptor = {key: record.get(key) for key in (
            "encoding", "tiktoken_version", "model_encoding_verified", "method")}
        if descriptor not in metadata:
            metadata.append(descriptor)
        if (descriptor != {"encoding": ENCODING, "tiktoken_version": TIKTOKEN_VERSION,
                           "model_encoding_verified": False,
                           "method": "sum_separately_encoded_text_parts"}
                or record.get("usage_complete") is not True
                or record.get("response_received") is not True):
            complete = False
        for key in totals:
            value = record.get(key)
            if type(value) is int and value >= 0:
                totals[key] += value
                observed[key] = True
            else:
                complete = False
    subtotal = {key: value if observed[key] else None for key, value in totals.items()}
    cost = None
    if complete and rates is not None:
        cost = (totals["input_tokens"] * rates["input_per_million"]
                + totals["output_tokens"] * rates["output_per_million"]) / 1e6
    return {
        "metric": "local_visible_text", "usage_complete": complete,
        "attempt_records": len(records), "known_records": len(known),
        "known_usage_subtotal": subtotal if known else None,
        "tokenizers": metadata, "models": sorted(models),
        "model_encoding_verified": False, "encoding_role": "unverified_model_proxy",
        "image_tokens": None,
        "hidden_reasoning_tokens": None, "hidden_instruction_tokens": None,
        "api_framing_tokens": None, "cached_input_tokens": None,
        "total_bill_tokens": None, "actual_fee": None,
        "visible_text_reference_cost_usd": cost, "pricing": rates,
    }
