#!/usr/bin/env python3
"""
model_registry.py — Version tracking for self-learning ML models.

Every trained model gets a version entry:
  - version string (v1, v2, ...)
  - training date
  - F1 / precision / recall / AUROC on held-out test set
  - n_human_labels used
  - n_synthetic_windows used
  - model file path
  - is_production (only one at a time)
  - parent_version (which model it was retrained from)

The registry is the source of truth for which model is in production.
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Optional


class ModelRegistry:
    def __init__(self, registry_path: str = "model_registry.json"):
        self.path = Path(registry_path)
        if self.path.exists():
            with open(self.path) as f:
                self.data = json.load(f)
        else:
            self.data = {"models": [], "production_version": None}

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def register(self, metrics: dict, n_human: int, n_synthetic: int,
                 model_path: str, parent_version: Optional[str] = None,
                 notes: str = "") -> str:
        """Register a new model version. Returns version string."""
        v_num = len(self.data["models"]) + 1
        version = f"v{v_num}"

        entry = {
            "version": version,
            "created": datetime.now().isoformat(),
            "metrics": metrics,
            "n_human_labels": n_human,
            "n_synthetic_windows": n_synthetic,
            "model_path": model_path,
            "parent_version": parent_version,
            "is_production": False,
            "notes": notes,
        }
        self.data["models"].append(entry)
        self.save()
        print(f"  Registered {version}: F1={metrics.get('f1', '?'):.3f}, "
              f"human_labels={n_human}")
        return version

    def get_production(self) -> Optional[dict]:
        """Get current production model entry."""
        pv = self.data["production_version"]
        if not pv:
            return None
        for m in self.data["models"]:
            if m["version"] == pv:
                return m
        return None

    def promote(self, version: str):
        """Promote a version to production."""
        # Demote current
        for m in self.data["models"]:
            m["is_production"] = (m["version"] == version)
        self.data["production_version"] = version
        self.save()
        print(f"  ✓ Promoted {version} to production")

    def compare(self, version_a: str, version_b: str,
                metric: str = "f1") -> dict:
        """Compare two versions. Returns which is better."""
        a = next(m for m in self.data["models"] if m["version"] == version_a)
        b = next(m for m in self.data["models"] if m["version"] == version_b)
        a_val = a["metrics"].get(metric, 0)
        b_val = b["metrics"].get(metric, 0)
        return {
            "metric": metric,
            "a": {"version": version_a, "value": a_val},
            "b": {"version": version_b, "value": b_val},
            "winner": version_b if b_val > a_val else version_a,
            "improvement": b_val - a_val,
        }

    def history(self):
        """Print version history."""
        print(f"\n{'Version':<10} {'F1':<8} {'Human':<8} {'Synthetic':<10} "
              f"{'Production':<12} {'Created'}")
        print("-" * 70)
        for m in self.data["models"]:
            prod = "★ PROD" if m["is_production"] else ""
            print(f"{m['version']:<10} "
                  f"{m['metrics'].get('f1', 0):<8.3f} "
                  f"{m['n_human_labels']:<8} "
                  f"{m['n_synthetic_windows']:<10} "
                  f"{prod:<12} "
                  f"{m['created'][:10]}")
