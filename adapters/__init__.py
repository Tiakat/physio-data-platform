"""Project adapters: bridge between the platform runner and real signal code.

An adapter exposes::

    def process_patient(raw_path: str, cfg, out_dir: str, log) -> dict

It reads one raw file, runs the project's signal processing, writes
``signals.parquet`` / ``features.parquet`` / ``figures/*.png`` into ``out_dir``
(using :mod:`physio_platform.store`), and returns a dict of summary scalars
(``n_samples``, ``duration_s``, ...). Any exception is caught by the runner
and recorded per patient.
"""
