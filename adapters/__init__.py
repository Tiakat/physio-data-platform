"""Project adapters: bridge between the platform runner and real signal code.

An adapter exposes::

    def process_patient(raw_path: str, cfg, out_dir: str, log) -> dict

It reads one raw file, runs the project's signal processing, writes
``signals.parquet`` / ``features.parquet`` / ``figures/*.png`` into
``out_dir`` (plain parquet files, same layout as tools/run_local.py's
``local_store/<PROJECT>/parquet/``), and returns a dict of summary scalars
(``n_samples``, ``duration_s``, ...). Any exception is caught by the runner
and recorded per patient.
"""
