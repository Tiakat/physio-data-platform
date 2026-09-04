# Physiological Research Data Platform

A private research data platform for ingesting and validating heterogeneous physiological
recordings from multiple research projects.

The repository contains source code and configuration only. Research data, credentials,
and other sensitive material are never stored in GitHub.

## Current phase

### Phase 1 — Data discovery and validation

Completed:

- Connected the development environment to Azure Blob Storage.
- Established the private GitHub repository.
- Defined the six active research projects.
- Restricted ingestion to project `Database/RawData` folders.
- Identified heterogeneous file formats and device sources.
- Implemented recursive data discovery.
- Implemented file-level validation.
- Implemented SHA-256 checksums.
- Implemented duplicate detection.
- Implemented project-specific configuration profiles.
- Implemented device-specific parsers/validators for supported formats.
- Tested Azure uploads with small and large files.
- Tested resumable/retry-style ingestion behaviour.
- Verified uploaded files against their SHA-256 checksums.
- Distinguished validation failures from missing modality coverage.
- Completed validation across all six active projects.

## Projects

The platform currently supports six active research projects:

- PROMISES
- IPAMS
- DEXREM
- V-RAPS
- SILVR
- ESMONOL

Project-specific characteristics and patient counts are maintained in the project
profiles and validation outputs rather than in this README.

Warnings are recorded separately from failures. For example, a missing
modality or device signal is not automatically treated as a corrupt file.

## Validation principles

The platform currently separates three concepts:

### Validation

Is the file intact, readable, and structurally valid?

### Coverage

Which devices and physiological modalities are actually present for a patient?

### Inclusion

Does a particular analysis require a modality that this patient does not have?

A missing modality is therefore not automatically an ingestion failure.

## Current supported data sources

The codebase currently contains parsers/validation logic for supported physiological
recording sources including:

- NOL / Medasense
- Infinity
- BetterCare
- Pump-related recordings

Additional formats are retained for future handling rather than being forced through
an unsupported parser.

## Repository structure

```text
backbone/
    config.py
    discover.py
    validate.py
    qc.py
    parsers/
        _common.py
        nol_medasense.py
        infinity.py
        bettercare.py
        pump.py

profiles/
    _variables.yaml
    promises.yaml
    ipams.yaml
    dexrem.yaml
    v-raps.yaml
    silvr.yaml
    esmonol.yaml

ingest/
    rules.yaml

tools/
    sync_dropbox.py
    validate_project.py
