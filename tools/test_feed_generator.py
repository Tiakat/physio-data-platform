# Test Feed Generator for liam-v2 physio-data window
# Creates a test feed JSON from PUBLIC data (VitalDB-style, synthetic)
# Purpose: validate the frontend renders correctly WITHOUT touching patient data
# This does NOT use K's data and does NOT go through the privacy gate
#
# Feed format (from components/physio-data/FeedView.tsx):
# {
#   "projects": {
#     "NAME": {
#       "n_exams": int|null,
#       "suppressed": bool,
#       "total_rows": int|null,
#       "qc_verdicts": {"verdict": count},
#       "ett_versions": {"version": count},
#       "signals": {
#         "signal": {"n_exams": int, "mean_duration_s": float, "total_hours": float, "null_fraction": float}
#       }
#     }
#   },
#   "generatedAt": "ISO timestamp"
# }

import json
from datetime import datetime, timezone

def generate_test_feed():
    """Generate a test feed with synthetic public data."""
    return {
        "projects": {
            "VITALDB_TEST": {
                "n_exams": 10,
                "suppressed": False,
                "total_rows": 360000,
                "qc_verdicts": {
                    "VALID": 340000,
                    "MISSING": 15000,
                    "OUT_OF_RANGE": 3000,
                    "SPIKE": 2000
                },
                "ett_versions": {},
                "signals": {
                    "BIS": {
                        "n_exams": 10,
                        "mean_duration_s": 7200,
                        "total_hours": 20.0,
                        "null_fraction": 0.02
                    },
                    "ECG_II": {
                        "n_exams": 10,
                        "mean_duration_s": 7200,
                        "total_hours": 20.0,
                        "null_fraction": 0.01
                    },
                    "ART": {
                        "n_exams": 8,
                        "mean_duration_s": 6800,
                        "total_hours": 15.1,
                        "null_fraction": 0.05
                    },
                    "PLETH": {
                        "n_exams": 10,
                        "mean_duration_s": 7200,
                        "total_hours": 20.0,
                        "null_fraction": 0.03
                    }
                }
            },
            "DEXREM": {
                "n_exams": None,
                "suppressed": True,
                "total_rows": None,
                "qc_verdicts": {},
                "ett_versions": {},
                "signals": {}
            }
        },
        "generatedAt": datetime.now(timezone.utc).isoformat()
    }

if __name__ == "__main__":
    feed = generate_test_feed()
    with open("test_feed.json", "w") as f:
        json.dump(feed, f, indent=2)
    print("Test feed written to test_feed.json")
    print("This is SYNTHETIC test data, not patient data.")
