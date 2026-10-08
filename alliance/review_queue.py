#!/usr/bin/env python3
"""
review_queue.py — Human review queue for uncertain ML predictions.

Takes windows where the model is uncertain (0.2 < P(artifact) < 0.7)
and presents them for expert labeling. Labels feed back into retraining.

Usage:
  # Generate queue from pipeline output
  python review_queue.py --build --input ml_results.csv --output review_queue.json

  # Review interactively (CLI)
  python review_queue.py --review --queue review_queue.json

  # Export labels for retraining
  python review_queue.py --export --queue review_queue.json --output labels.csv

Labels: VALID, SPIKE, DROPOUT, FLATLINE, NOISE, BASELINE_SHIFT,
        SATURATION, DEVICE_ARTIFACT, CLINICALLY_ABNORMAL, UNKNOWN
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime
import pandas as pd

VALID_LABELS = [
    "VALID",
    "SPIKE",
    "DROPOUT",
    "FLATLINE",
    "NOISE",
    "BASELINE_SHIFT",
    "SATURATION",
    "DEVICE_ARTIFACT",
    "CLINICALLY_ABNORMAL",  # real but unusual physiology — DO NOT DELETE
    "UNKNOWN",
]

# Uncertain range: model isn't confident either way
UNCERTAIN_LOW = 0.20
UNCERTAIN_HIGH = 0.70


def build_queue(ml_results_path: str, output_path: str,
                low: float = UNCERTAIN_LOW, high: float = UNCERTAIN_HIGH):
    """Build review queue from ML pipeline output."""
    df = pd.read_csv(ml_results_path)

    # Uncertain windows
    uncertain = df[(df["artifact_prob"] >= low) & (df["artifact_prob"] <= high)]

    # Also include high-confidence artifacts for spot-checking (5% sample)
    confident = df[df["artifact_prob"] > high]
    if len(confident):
        n_spot = min(len(confident), max(1, len(confident) // 20))
        spot_check = confident.sample(n_spot, random_state=42)
    else:
        spot_check = pd.DataFrame()

    queue = []
    for _, row in uncertain.iterrows():
        queue.append({
            "window": int(row["window"]),
            "start_idx": int(row["start_idx"]),
            "end_idx": int(row["end_idx"]),
            "artifact_prob": float(row["artifact_prob"]),
            "model_pred": int(row["artifact_pred"]),
            "reason": "uncertain",
            "label": None,
            "reviewed_at": None,
            "reviewer": None,
        })
    for _, row in spot_check.iterrows():
        queue.append({
            "window": int(row["window"]),
            "start_idx": int(row["start_idx"]),
            "end_idx": int(row["end_idx"]),
            "artifact_prob": float(row["artifact_prob"]),
            "model_pred": int(row["artifact_pred"]),
            "reason": "spot_check",
            "label": None,
            "reviewed_at": None,
            "reviewer": None,
        })

    # Sort by uncertainty (closest to 0.5 first = most uncertain)
    queue.sort(key=lambda x: abs(x["artifact_prob"] - 0.5))

    with open(output_path, "w") as f:
        json.dump({
            "created": datetime.now().isoformat(),
            "n_uncertain": len(uncertain),
            "n_spot_check": len(spot_check),
            "items": queue,
        }, f, indent=2)

    print(f"Queue built: {len(uncertain)} uncertain + {len(spot_check)} spot-check")
    print(f"Saved to {output_path}")
    return queue


def review_cli(queue_path: str):
    """Interactive CLI review."""
    with open(queue_path) as f:
        data = json.load(f)

    items = data["items"]
    pending = [i for i in items if i["label"] is None]
    print(f"\n{'='*60}")
    print(f"REVIEW QUEUE: {len(pending)} pending / {len(items)} total")
    print(f"{'='*60}")
    print(f"\nLabels: {', '.join(VALID_LABELS)}")
    print("Commands: [label], s=skip, q=quit\n")

    reviewer = input("Your name/initials: ").strip() or "reviewer"

    reviewed = 0
    for item in pending:
        print(f"\n--- Window {item['window']} "
              f"(samples {item['start_idx']}-{item['end_idx']}) ---")
        print(f"  Model P(artifact) = {item['artifact_prob']:.3f} "
              f"→ pred={'ARTIFACT' if item['model_pred'] else 'VALID'}")
        print(f"  Reason: {item['reason']}")

        while True:
            ans = input("  Label: ").strip().upper()
            if ans == "Q":
                print(f"\nSaved. {reviewed} reviewed this session.")
                with open(queue_path, "w") as f:
                    json.dump(data, f, indent=2)
                return
            if ans == "S":
                break
            if ans in VALID_LABELS:
                item["label"] = ans
                item["reviewed_at"] = datetime.now().isoformat()
                item["reviewer"] = reviewer
                reviewed += 1
                print(f"  ✓ Labeled {ans} ({reviewed} this session)")
                break
            print(f"  Invalid. Choose from: {', '.join(VALID_LABELS)}")

    with open(queue_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n✓ Queue complete. {reviewed} labeled.")


def export_labels(queue_path: str, output_path: str):
    """Export reviewed labels to CSV for retraining."""
    with open(queue_path) as f:
        data = json.load(f)

    labeled = [i for i in data["items"] if i["label"] is not None]
    if not labeled:
        print("No labeled items yet.")
        return

    df = pd.DataFrame(labeled)
    # Binary label for retraining
    df["is_artifact"] = (~df["label"].isin(["VALID", "CLINICALLY_ABNORMAL", "UNKNOWN"])).astype(int)
    df.to_csv(output_path, index=False)
    print(f"Exported {len(df)} labels to {output_path}")
    print(f"  Artifact: {(df['is_artifact']==1).sum()}, "
          f"Valid: {(df['is_artifact']==0).sum()}")
    # Flag clinically abnormal separately — these are NOT artifacts
    n_clin = (df["label"] == "CLINICALLY_ABNORMAL").sum()
    if n_clin:
        print(f"  ⚠ {n_clin} labeled CLINICALLY_ABNORMAL (preserved, not deleted)")


def main():
    p = argparse.ArgumentParser(description="Human review queue")
    p.add_argument("--build", action="store_true", help="Build queue from ML output")
    p.add_argument("--review", action="store_true", help="Interactive review")
    p.add_argument("--export", action="store_true", help="Export labels to CSV")
    p.add_argument("--input", help="ML results CSV (for --build)")
    p.add_argument("--queue", default="review_queue.json", help="Queue JSON file")
    p.add_argument("--output", help="Output file")
    args = p.parse_args()

    if args.build:
        if not args.input or not args.output:
            p.error("--build needs --input and --output")
        build_queue(args.input, args.output)
    elif args.review:
        review_cli(args.queue)
    elif args.export:
        if not args.output:
            p.error("--export needs --output")
        export_labels(args.queue, args.output)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
