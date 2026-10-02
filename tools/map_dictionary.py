"""Map the raw column census to canonical variables (confidence-tiered).

Reads column_dictionary.csv (from the raw-source header census) and fills
canonical_variable / domain / signal_type / unit / sampling_rate where a
confident match exists against profiles/_variables.yaml. Everything else
keeps canonical empty and gets a review reason -- never forced.

Confidence tiers:
  alias_exact   -- normalized name hits a dictionary alias exactly
  alias_base    -- duplicate-suffixed (HR.1) or unit-stripped base hits
  classified    -- no signal match; rule-based domain (demographic,
                   ventilator_setting, anesthesia_setting, alarm, metadata,
                   exposure, event)
  review        -- unknown, queued for human classification

Usage:
  python tools/map_dictionary.py --dict column_dictionary.csv \
      --variables profiles/_variables.yaml --out column_dictionary_mapped.csv \
      --report mapping_report.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

DUP_SUFFIX = re.compile(r"\.\d+$")
WS = re.compile(r"\s+")
PAREN_CUT = re.compile(r"\s*\(.*$")

# Rule-based domain classifiers (ordered). Each: (domain, [patterns]).
DOMAIN_RULES = [
    ("demographic", [r"\bage\b", r"\bweight\b", r"\bheight\b", r"\bgender\b",
                     r"\bsex\b", r"\bbmi\b", r"\bdevicename\b"]),
    ("ventilator_setting_extra", [r"\bcomplian", r"\belastance",
                     r"\bresist", r"\bpressure support",
                     r"\bdpsupp\b", r"\bhose\b", r"\bcircuit\b",
                     r"\bcons\b", r"\btot\b", r"\bfg \b"]),
    ("ventilator_setting", [r"\btplat\b", r"\bppeak\b", r"\bpeep\b",
                             r"\bventil", r"\bresp(\.| )rate.*set",
                             r"\btidal", r"\bair cons", r"\bo2 cons",
                             r"\bfresh gas", r"\bflow.*insp"]),
    ("anesthesia_setting", [r"\banesthes", r"\bsevoflur", r"\bdesflur",
                             r"\bisoflur", r"\bagent\b", r"\bmac\b",
                             r"\bgas\b"]),
    ("alarm", [r"\balarm\b", r"\bapn", r"\bapnea\b", r"\baudio pause"]),
    ("event", [r"\bannotation", r"\bevent\b", r"\bmarker\b"]),
    ("exposure", [r"\bdose\b", r"\binfusion", r"\bbolus\b", r"\bpump\b",
                  r"\bdrug\b", r"\brate\b.*ml", r"\bvolume\b.*infus"]),
    ("metadata", [r"\btime\b", r"\bdate\b", r"\bpatient categor",
                  r"\babs time", r"\brelative time", r"\bfile\b",
                  r"\bsource\b"]),
]

SIGNAL_TYPE_HINTS = {
    "waveform": [r"\becg\b", r"\bpleth\b", r"\bair flow\b", r"\bco2\b.*wave",
                 r"\bpressure\b.*wave", r"\beeg\b"],
    "parameter": [r"\bhr\b", r"\bspo2\b", r"\bnibp\b", r"\bart\b",
                  r"\btemp\b", r"\bnol\b", r"\bbis\b"],
}


def _clean(name: str) -> str:
    s = DUP_SUFFIX.sub("", name or "")
    return WS.sub(" ", s).strip().upper()


def normalize(name: str) -> str:
    """Exact normalized form (parentheses kept)."""
    return _clean(name)


def normalize_base(name: str) -> str:
    """Unit-stripped form: cut everything from the first '('.

    Vendor unit segments always run to end of name, e.g.
    "ART D (mm(hg)^^ISO+)" -> "ART D". Nested parens make regex
    stripping unreliable, so cut at the first paren instead.
    """
    return _clean(PAREN_CUT.sub("", name or ""))

# Dräger Infinity standard parameters: (domain, note).
INFINITY_KNOWN = {
    "MV": ("ventilator_setting", "minute volume"),
    "MVE": ("ventilator_setting", "expiratory minute volume"),
    "MVSPON": ("ventilator_setting", "spontaneous minute volume"),
    "PIP": ("ventilator_setting", "peak inspiratory pressure"),
    "PMEAN": ("ventilator_setting", "mean airway pressure"),
    "PPLAT": ("ventilator_setting", "plateau pressure"),
    "RRC": ("ventilator_setting", "respiratory rate control? verify"),
    "RRMAND": ("ventilator_setting", "mandatory respiratory rate"),
    "RRSPON": ("ventilator_setting", "spontaneous respiratory rate"),
    "RRP": ("ventilator_setting", "respiratory rate parameter"),
    "VTEMAND": ("ventilator_setting", "mandatory tidal volume"),
    "VTESPON": ("ventilator_setting", "spontaneous tidal volume"),
    "FIO2": ("ventilator_setting", "fraction of inspired O2"),
    "INISO": ("ventilator_setting", "initial isoflurane? verify"),
    "STANDBY": ("metadata", "ventilator standby state"),
    "VENT. MODE": ("ventilator_setting", "ventilation mode"),
    "VENT MODE": ("ventilator_setting", "ventilation mode"),
    "LEAK BS": ("ventilator_setting", "leak (body surface?) verify"),
    "TCASE": ("metadata", "test/case marker? verify"),
    "CO2 UNIT": ("metadata", "CO2 module identifier"),
    "CHOSE": ("ventilator_setting", "compliance hose? verify"),
    "STV": ("physiological_signal", "ST segment value? verify lead"),
    "STV+": ("physiological_signal", "ST segment value? verify lead"),
    "STAVF": ("physiological_signal", "ST aVF"),
    "STAVR": ("physiological_signal", "ST aVR"),
    "PVC/MIN": ("event", "premature ventricular contractions per min"),
    "%PACED": ("event", "percent paced beats"),
    "V'O2": ("physiological_signal", "O2 consumption? verify"),
    "ETN2O": ("physiological_signal", "end-tidal N2O"),
    "ETO2": ("physiological_signal", "end-tidal O2"),
    "ETPRIMA": ("anesthesia_setting", "end-tidal primary agent? verify"),
    "ETSECA": ("anesthesia_setting", "end-tidal secondary agent? verify"),
    "INPRIMA": ("anesthesia_setting", "inspired primary agent? verify"),
    "INSEV": ("anesthesia_setting", "inspired sevoflurane"),
    "INDES": ("anesthesia_setting", "inspired desflurane"),
    "INHAL": ("anesthesia_setting", "inspired halothane"),
    "INXMAC": ("anesthesia_setting", "inspired xMAC"),
    "INSP. TERM. SET": ("ventilator_setting", "inspiratory termination setting"),
    "DO2": ("physiological_signal", "oxygen delivery? verify"),
    "E": ("ventilator_setting", "elastance"),
    "STI": ("physiological_signal", "ST lead I"),
    "STII": ("physiological_signal", "ST lead II"),
    "STIII": ("physiological_signal", "ST lead III"),
    "STAVL": ("physiological_signal", "ST aVL"),
    "MVMAND": ("ventilator_setting", "mandatory minute volume"),
    "PMAX SET": ("ventilator_setting", "max pressure setting"),
    "SLOPE SET": ("ventilator_setting", "pressure slope setting"),
    "RR SET": ("ventilator_setting", "respiratory rate setting"),
    "LEAK BS M/S": ("ventilator_setting", "leak"),
    "DEVICE CHECK": ("metadata", "device self-check flag"),
}

DICTIONARY_CANDIDATES = {
    "BSR": "burst suppression ratio (BIS family)",
    "EMG": "electromyography (BIS family)",
    "SEF": "spectral edge frequency (BIS family)",
    "MF": "median frequency (BIS family)",
    "POW": "total power (BIS family)",
    "DO2": "oxygen delivery (derived?)",
    "VO2": "oxygen consumption (derived?)",
    "RR": "respiratory rate",
    "RESP": "respiratory (rate or waveform? ambiguous)",
}


def is_truncated(name: str) -> bool:
    s = name or ""
    return "(" in s and ")" not in s[s.index("("):]


def truncation_base(name: str) -> str:
    return normalize(name.split("(")[0])


def build_alias_index(variables: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (exact_index, base_index)."""
    exact: dict[str, str] = {}
    base: dict[str, str] = {}
    for canon, spec in variables.items():
        if not isinstance(spec, dict):
            continue
        exact[normalize(canon)] = canon
        base.setdefault(normalize_base(canon), canon)
        for a in spec.get("aliases", []) or []:
            exact.setdefault(normalize(str(a)), canon)
            base.setdefault(normalize_base(str(a)), canon)
    return exact, base


def classify_domain(norm: str, source: str) -> tuple[str, str]:
    for domain, patterns in DOMAIN_RULES:
        for p in patterns:
            if re.search(p, norm, re.IGNORECASE):
                return domain, f"rule:{p}"
    if source == "pump":
        return "exposure", "rule:source=pump"
    if source == "nol":
        return "event_or_signal", "rule:source=nol"
    return "unknown", "no-rule-matched"


def signal_type_hint(norm: str) -> str:
    for st, patterns in SIGNAL_TYPE_HINTS.items():
        for p in patterns:
            if re.search(p, norm, re.IGNORECASE):
                return st
    return "unknown"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Confidence-tiered dictionary mapping.")
    ap.add_argument("--dict", required=True)
    ap.add_argument("--variables", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args(argv)

    import yaml
    variables = (yaml.safe_load(Path(args.variables).read_text(encoding="utf-8"))
                 or {}).get("variables", {})
    alias_exact_idx, alias_base_idx = build_alias_index(variables)

    with open(args.dict, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    fieldnames = list(rows[0].keys()) + ["mapping_confidence", "review_reason"]

    counts: dict[str, int] = {}
    for r in rows:
        if r.get("canonical_variable"):
            r["mapping_confidence"] = "preexisting"
            r["review_reason"] = ""
            counts["preexisting"] = counts.get("preexisting", 0) + 1
            continue
        original = r.get("original_column", "")
        norm = normalize(original)
        canon = alias_exact_idx.get(norm)
        tier = "alias_exact"
        if not canon:
            canon = alias_base_idx.get(normalize_base(original))
            tier = "alias_base"
        if canon:
            spec = variables[canon]
            r["canonical_variable"] = canon
            r["domain"] = "physiological_signal"
            r["signal_type"] = signal_type_hint(norm)
            r["unit"] = spec.get("unit", "") or r.get("unit", "")
            r["sampling_rate"] = r.get("sampling_rate", "") or "device_default"
            r["mapping_confidence"] = tier
            r["review_reason"] = ""
            counts[tier] = counts.get(tier, 0) + 1
            continue
        domain, reason = classify_domain(norm, r.get("source", ""))
        r["domain"] = domain if domain != "unknown" else r.get("domain", "")
        r["signal_type"] = ("unknown" if domain in ("unknown", "event_or_signal")
                            else signal_type_hint(norm))
        base_full = normalize_base(original)
        base_tok = base_full.split(" ")[0]
        inf_key = (base_full if base_full in INFINITY_KNOWN
                   else base_tok if base_tok in INFINITY_KNOWN else None)
        if domain == "unknown" and inf_key:
            dom, note = INFINITY_KNOWN[inf_key]
            r["domain"] = dom
            r["mapping_confidence"] = "classified"
            r["review_reason"] = f"Infinity param: {note}; needs signal mapping"
            counts["classified"] = counts.get("classified", 0) + 1
            continue
        if not (original or "").strip():
            r["mapping_confidence"] = "review"
            r["review_reason"] = "empty column name in source header; data quality"
            counts["review"] = counts.get("review", 0) + 1
            continue
        if domain == "unknown":
            r["mapping_confidence"] = "review"
            cand = (base_full if base_full in DICTIONARY_CANDIDATES
                    else base_tok if base_tok in DICTIONARY_CANDIDATES else None)
            if cand:
                r["review_reason"] = (
                    f"candidate dictionary entry: "
                    f"{DICTIONARY_CANDIDATES[cand]}")
            else:
                r["review_reason"] = f"no dictionary alias; {reason}"
        else:
            r["mapping_confidence"] = "classified"
            r["review_reason"] = f"domain={domain}; {reason}; needs signal mapping"
        counts[r["mapping_confidence"]] = counts.get(r["mapping_confidence"], 0) + 1

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    report = {"tool": "map_dictionary",
              "entries": len(rows),
              "by_confidence": counts,
              "mapped_canonical": sum(1 for r in rows if r["canonical_variable"]),
              "still_unmapped": sum(1 for r in rows if not r["canonical_variable"])}
    Path(args.report).write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
