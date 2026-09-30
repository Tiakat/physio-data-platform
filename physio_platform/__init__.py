"""physio-data-platform: automated processing for hospital physiological data.

Pipeline stages:
    Dropbox (raw) -> ingest -> per-patient processing -> Parquet store
                  -> cohort stats -> website feed

Each project is driven by a YAML config in ``projects/``; the actual signal
processing for a project lives in an *adapter* (see ``adapters/``).
"""

__version__ = "0.1.0"
