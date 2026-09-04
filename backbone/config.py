"""
Profile loader.

A profile is a YAML file describing one project. Profiles may inherit from
another profile with `extends:`, so five of the six projects are a dozen lines.

The pipeline reads profiles. It contains no project specific logic anywhere.
"""

from __future__ import annotations

import copy
import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

PROFILE_DIR = Path(os.environ.get("PROFILE_DIR", Path(__file__).parent.parent / "profiles"))


def _deep_merge(base: dict, override: dict) -> dict:
    """Override wins for scalars and lists; dictionaries merge key by key."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@lru_cache(maxsize=None)
def load_variables() -> dict:
    """The shared data dictionary."""
    with open(PROFILE_DIR / "_variables.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=None)
def load_profile(name: str) -> dict:
    """
    Load a profile by file name or project code, resolving `extends` chains.

    load_profile("ipams.yaml") and load_profile("IPAMS") both work.
    """
    path = PROFILE_DIR / name if name.endswith(".yaml") else None
    if path is None or not path.exists():
        for candidate in sorted(PROFILE_DIR.glob("*.yaml")):
            if candidate.name.startswith("_"):
                continue
            with open(candidate, encoding="utf-8") as fh:
                head = yaml.safe_load(fh) or {}
            if head.get("project", {}).get("code", "").upper() == name.upper():
                path = candidate
                break
    if path is None or not path.exists():
        raise FileNotFoundError(f"No profile for {name!r} in {PROFILE_DIR}")

    with open(path, encoding="utf-8") as fh:
        profile = yaml.safe_load(fh) or {}

    parent = profile.pop("extends", None)
    if parent:
        profile = _deep_merge(load_profile(parent), profile)

    profile["_file"] = path.name
    profile["_variables"] = load_variables()
    return profile


def list_profiles() -> list[dict]:
    out = []
    for path in sorted(PROFILE_DIR.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        out.append(load_profile(path.name))
    return out


# --------------------------------------------------------------- alias index

def alias_index(profile: dict) -> dict[str, str]:
    """
    Map every accepted source column name to its standard variable name.

    Matching is deliberately forgiving: case, surrounding whitespace and
    repeated internal spaces are all ignored, because the archive contains
    "Better Care", "BetterCare" and "bettercare" for the same thing.
    """
    index: dict[str, str] = {}
    for standard, spec in profile["_variables"]["variables"].items():
        index[normalise_key(standard)] = standard
        for alias in spec.get("aliases", []):
            index[normalise_key(alias)] = standard
    return index


def normalise_key(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().strip('"').lower()


def variable_spec(profile: dict, standard: str) -> dict:
    return profile["_variables"]["variables"].get(standard, {})


def missing_codes(profile: dict) -> set:
    return set(profile["_variables"].get("global_missing_codes", []))


# --------------------------------------------------------------- file tiers

def classify_file(profile: dict, filename: str) -> tuple[str, str | None, str | None]:
    """
    Decide what to do with a file.

    Returns (tier, device_code, parser_name).
      tier A -> parse it
      tier B -> keep it, do not parse it
      tier C -> ignore it entirely

    A tier B file is never an ingestion failure. That single rule is what stops
    one PDF from breaking a whole patient.
    """
    from fnmatch import fnmatch

    name = os.path.basename(filename)
    tiers = profile.get("tiers", {})

    for pattern in tiers.get("C_ignored", []):
        if fnmatch(name, pattern):
            return "C", None, None

    for rule in tiers.get("A_parsed", []):
        if fnmatch(name, rule["pattern"]):
            return "A", rule.get("device"), rule.get("parser")

    for pattern in tiers.get("B_kept", []):
        if fnmatch(name.lower(), pattern.lower()):
            return "B", None, None

    # Unknown extension. Keep it rather than lose it, and make it findable.
    return "B", None, None


def device_from_dirname(profile: dict, dirname: str) -> str | None:
    mapping = profile.get("layout", {}).get("device_dir_map", {})
    return mapping.get(normalise_key(dirname))
