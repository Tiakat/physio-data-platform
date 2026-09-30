"""Tests for physio_platform.config: loading and validation of project YAML files."""
import textwrap

import pytest
import yaml

from physio_platform.config import ConfigError, load_project


def write_cfg(tmp_path, **overrides):
    base = {
        "project": "test-proj",
        "description": "test",
        "dropbox": {"source_dir": "/Projets actifs/test-proj/raw"},
        "sampling_rate_hz": 200.0,
        "channels": ["ECG"],
        "processing": {"adapter": "adapters.x:process_patient"},
    }
    base.update(overrides)
    p = tmp_path / "test-proj.yaml"
    p.write_text(yaml.safe_dump(base))
    return p


def test_load_minimal_valid(tmp_path):
    cfg = load_project(write_cfg(tmp_path))
    assert cfg.name == "test-proj"
    assert cfg.sampling_rate_hz == 200.0
    assert cfg.dropbox.file_pattern == "*.csv"  # default
    assert cfg.processing.chunk_seconds == 600.0  # default
    assert len(cfg.fingerprint) == 12


def test_fingerprint_changes_with_config(tmp_path):
    c1 = load_project(write_cfg(tmp_path))
    c2 = load_project(write_cfg(tmp_path, sampling_rate_hz=500.0))
    assert c1.fingerprint != c2.fingerprint


def test_missing_project_key(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump({"description": "no name"}))
    with pytest.raises(ConfigError):
        load_project(p)


def test_missing_source_dir(tmp_path):
    with pytest.raises(ConfigError):
        load_project(write_cfg(tmp_path, dropbox={}))


def test_bad_sampling_rate(tmp_path):
    with pytest.raises(ConfigError):
        load_project(write_cfg(tmp_path, sampling_rate_hz=0))


def test_bad_adapter_spec(tmp_path):
    with pytest.raises(ConfigError):
        load_project(write_cfg(tmp_path,
                               processing={"adapter": "not-a-spec"}))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_project(tmp_path / "does-not-exist.yaml")


def test_example_config_loads():
    cfg = load_project("projects/intraop-eeg.yaml")
    assert cfg.name == "intraop-eeg"
    assert cfg.processing.adapter == "adapters.intraop_eeg:process_patient"
