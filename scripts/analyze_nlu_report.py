#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple


def load_call_records(calls_dir: Path) -> List[Dict[str, Any]]:
    if not calls_dir.exists():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(calls_dir.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                records.append(json.load(f))
        except Exception:
            # Keep report generation robust to partial/corrupt files.
            continue
    return records


def build_report(records: List[Dict[str, Any]], *, low_confidence_threshold: float) -> Dict[str, Any]:
    status_counts = Counter()
    outcome_counts = Counter()
    nlu_intent_counts = Counter()
    assistant_intent_counts = Counter()
    low_confidence_counts = Counter()
    intent_to_assistant: Dict[str, Counter] = defaultdict(Counter)

    total_turns = 0
    turns_with_nlu = 0

    for record in records:
        status_counts[str(record.get("status", "unknown"))] += 1
        outcome = record.get("final_outcome_code")
        if outcome:
            outcome_counts[str(outcome)] += 1

        for turn in record.get("turns", []):
            total_turns += 1
            nlu_intent = turn.get("nlu_intent")
            nlu_conf = turn.get("nlu_confidence")
            assistant_intent = str(turn.get("assistant_intent", "unknown"))
            assistant_intent_counts[assistant_intent] += 1

            if nlu_intent is None:
                continue

            nlu_intent = str(nlu_intent)
            turns_with_nlu += 1
            nlu_intent_counts[nlu_intent] += 1
            intent_to_assistant[nlu_intent][assistant_intent] += 1

            if isinstance(nlu_conf, (int, float)) and float(nlu_conf) < low_confidence_threshold:
                low_confidence_counts[nlu_intent] += 1

    return {
        "calls_total": len(records),
        "status_counts": dict(status_counts),
        "outcome_counts": dict(outcome_counts),
        "total_turns": total_turns,
        "turns_with_nlu": turns_with_nlu,
        "nlu_intent_counts": dict(nlu_intent_counts),
        "assistant_intent_counts": dict(assistant_intent_counts),
        "low_confidence_counts": dict(low_confidence_counts),
        "intent_to_assistant": {k: dict(v) for k, v in intent_to_assistant.items()},
        "low_confidence_threshold": low_confidence_threshold,
    }


def _top_keys(counter_like: Dict[str, int], limit: int) -> List[str]:
    pairs = sorted(counter_like.items(), key=lambda x: (-x[1], x[0]))
    return [k for k, _ in pairs[:limit]]


def _format_two_col_table(rows: List[Tuple[str, str]], *, left_label: str, right_label: str) -> str:
    left_w = max([len(left_label)] + [len(r[0]) for r in rows]) if rows else len(left_label)
    right_w = max([len(right_label)] + [len(r[1]) for r in rows]) if rows else len(right_label)
    sep = f"+-{'-' * left_w}-+-{'-' * right_w}-+"
    out = [sep, f"| {left_label.ljust(left_w)} | {right_label.ljust(right_w)} |", sep]
    for left, right in rows:
        out.append(f"| {left.ljust(left_w)} | {right.ljust(right_w)} |")
    out.append(sep)
    return "\n".join(out)


def _format_matrix(matrix: Dict[str, Dict[str, int]], *, row_limit: int, col_limit: int) -> str:
    if not matrix:
        return "(no NLU rows)"

    row_totals = {row: sum(cols.values()) for row, cols in matrix.items()}
    rows = _top_keys(row_totals, row_limit)

    col_counter: Counter = Counter()
    for cols in matrix.values():
        for c, v in cols.items():
            col_counter[c] += v
    cols = _top_keys(dict(col_counter), col_limit)

    # Build simple fixed-width grid.
    row_label_w = max([len("nlu_intent")] + [len(r) for r in rows])
    col_widths = {c: max(len(c), 5) for c in cols}

    header = ["nlu_intent".ljust(row_label_w)] + [c.rjust(col_widths[c]) for c in cols] + [" total"]
    lines = [" | ".join(header), "-" * (sum(len(h) for h in header) + 3 * (len(header) - 1))]

    for r in rows:
        row_vals = []
        row_total = 0
        for c in cols:
            val = matrix.get(r, {}).get(c, 0)
            row_total += val
            row_vals.append(str(val).rjust(col_widths[c]))
        lines.append(" | ".join([r.ljust(row_label_w)] + row_vals + [str(row_total).rjust(5)]))

    return "\n".join(lines)


def format_report(report: Dict[str, Any], *, top_n: int) -> str:
    lines: List[str] = []
    lines.append("NLU Report")
    lines.append(f"calls_total={report['calls_total']}")
    lines.append(f"turns_total={report['total_turns']}, turns_with_nlu={report['turns_with_nlu']}")
    lines.append("")

    status_rows = [(k, str(v)) for k, v in sorted(report["status_counts"].items(), key=lambda x: (-x[1], x[0]))]
    lines.append("Call Status")
    lines.append(_format_two_col_table(status_rows, left_label="status", right_label="count") if status_rows else "(no data)")
    lines.append("")

    outcome_rows = [(k, str(v)) for k, v in sorted(report["outcome_counts"].items(), key=lambda x: (-x[1], x[0]))[:top_n]]
    lines.append("Top Outcomes")
    lines.append(_format_two_col_table(outcome_rows, left_label="outcome", right_label="count") if outcome_rows else "(no outcomes)")
    lines.append("")

    intent_rows: List[Tuple[str, str]] = []
    intent_counts = report["nlu_intent_counts"]
    low_counts = report["low_confidence_counts"]
    threshold = float(report["low_confidence_threshold"])
    for intent, count in sorted(intent_counts.items(), key=lambda x: (-x[1], x[0]))[:top_n]:
        low = int(low_counts.get(intent, 0))
        rate = (low / count * 100.0) if count else 0.0
        intent_rows.append((intent, f"count={count}, low<{threshold:.2f}={low} ({rate:.1f}%)"))
    lines.append("Intent Confidence")
    lines.append(_format_two_col_table(intent_rows, left_label="intent", right_label="stats") if intent_rows else "(no intents)")
    lines.append("")

    lines.append("Intent -> Assistant (Confusion-Style Matrix)")
    lines.append(_format_matrix(report["intent_to_assistant"], row_limit=top_n, col_limit=top_n))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze NLU intent confidence from persisted call logs.")
    parser.add_argument("--calls-dir", type=str, default="runtime/calls")
    parser.add_argument("--low-confidence-threshold", type=float, default=0.45)
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--json", action="store_true", help="Print JSON report instead of table output.")
    args = parser.parse_args()

    records = load_call_records(Path(args.calls_dir))
    report = build_report(records, low_confidence_threshold=args.low_confidence_threshold)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report, top_n=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

