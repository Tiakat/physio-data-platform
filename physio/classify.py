"""
Classify ANY research file from its path + name alone.

    >>> from physio.classify import Classifier
    >>> c = Classifier()
    >>> r = c.classify("DEXREM/1/20250509091611.infinity.data.OR^^BLOC07.csv")
    >>> r.source, r.stage, r.subject, r.session_date
    ('Infinity', 'RAW', '1', '2025-05-09')

The folder a file sits in is only *evidence*, never the truth: the file-name
signature (config/sources.yaml) decides the source when it is specific, so a
loose file with no source folder is still recognised.

CLI:
    python -m physio.classify azure_full_inventory.csv --out reports/phase1
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"

# Folders that only wrap content; they carry no subject/source meaning.
WRAPPERS = {"database", "rawdata", "extracteddata", "extracted data", "exctracteddata",
            "included patients", "included patient", "processeddata", "data",
            "the infinity"}          # PROMISES: misnamed container holding every device

MONTHS_FR = {"janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5,
             "juin": 6, "juillet": 7, "aout": 8, "août": 8, "septembre": 9,
             "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12}

LAYER = {"RAW": "RawData", "DOCUMENT": "RawData", "EXTRACTED": "ExtractedData",
         "DERIVED": "ProcessedData", "METADATA": "Metadata"}


def norm(s: str) -> str:
    """lower-case, strip accents, collapse separators, drop trailing digits."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[\s_\-]+", " ", s.strip().lower())
    return re.sub(r"\s*\d+$", "", s).strip()


@dataclass
class Result:
    path: str
    project: str | None = None
    subject: str | None = None
    subject_evidence: str = "unresolved"      # folder | filename | unresolved
    session_date: str | None = None           # YYYY-MM-DD
    session_start: str | None = None          # ISO datetime when known
    source: str | None = None
    source_evidence: str = "none"             # filename | folder | both | conflict | none
    stage: str = "UNKNOWN"
    kind: str | None = None
    rule_id: str | None = None
    confidence: str = "none"
    in_source_folder: bool = False
    loose: bool = False                       # not inside any recognised source folder
    canonical_path: str | None = None
    notes: list = field(default_factory=list)

    def row(self) -> dict:
        d = asdict(self)
        d["notes"] = "; ".join(self.notes)
        return d


class Classifier:
    def __init__(self, sources_yaml: Path = CONFIG / "sources.yaml",
                 projects_yaml: Path = CONFIG / "projects.yaml"):
        cfg = yaml.safe_load(Path(sources_yaml).read_text(encoding="utf-8"))
        self.rules = []
        for r in cfg["rules"]:
            self.rules.append({**r, "rx": re.compile(r["pattern"], re.IGNORECASE)})
        self.alias = {}
        self.folder_rx = []                     # (compiled regex, source)
        for name, s in cfg["sources"].items():
            self.alias[norm(name)] = name
            for a in s.get("folder_aliases", []):
                self.alias[norm(a)] = name
            for fp in s.get("folder_patterns", []):
                self.folder_rx.append((re.compile(fp, re.IGNORECASE), name))
        self.derived_folders = {norm(x) for x in cfg.get("derived_folders", [])}
        self.ignore_folders = {norm(x) for x in cfg.get("ignore_folders", [])}
        self.ignore_names = {x.lower() for x in cfg.get("ignore_names", [])}
        self.ignore_prefixes = tuple(cfg.get("ignore_prefixes", []))

        pcfg = yaml.safe_load(Path(projects_yaml).read_text(encoding="utf-8"))
        default_pats = pcfg.get("defaults", {}).get("subject_patterns", [])
        self.projects = pcfg["projects"]
        self.subject_rx = {}
        for code, p in self.projects.items():
            pats = (p.get("subject_patterns") or []) + default_pats
            self.subject_rx[code] = [re.compile(x, re.IGNORECASE) for x in pats]
        self.default_subject_rx = [re.compile(x, re.IGNORECASE) for x in default_pats]

    def folder_source(self, folder: str) -> str | None:
        s = self.alias.get(norm(folder))
        if s:
            return s
        for rx, name in self.folder_rx:
            if rx.match(folder.strip()):
                return name
        return None

    # ------------------------------------------------------------------
    def classify(self, path: str, project: str | None = None) -> Result:
        """`path` = 'PROJECT/…/file' (Azure style) or relative path + `project`."""
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if project is None and parts:
            project, parts = parts[0], parts[1:]
        res = Result(path=path, project=project)
        if project not in self.projects:
            res.notes.append("UNCONFIGURED project (not in config/projects.yaml)")
        if not parts:
            res.notes.append("empty path")
            return res
        fname, folders = parts[-1], parts[:-1]

        if fname.lower() in self.ignore_names or fname.startswith(self.ignore_prefixes):
            res.stage, res.notes = "IGNORE", ["system/temp file"]
            return res
        if any(norm(f) in self.ignore_folders for f in folders):
            res.stage = "IGNORE"
            res.notes.append("inside a documents/regulatory folder")
            return res

        # 1) file-name signature
        rule, m = None, None
        for r in self.rules:
            if r.get("projects") and project not in r["projects"]:
                continue
            m = r["rx"].search(fname)
            if m:
                rule = r
                break

        # 2) folder evidence (deepest source folder wins)
        folder_src, src_idx = None, -1
        for i in range(len(folders) - 1, -1, -1):
            s = self.folder_source(folders[i])
            if s:
                folder_src, src_idx = s, i
                break
        res.in_source_folder = folder_src is not None

        # 3) decide source
        name_src = rule.get("source") if rule else None
        if name_src and folder_src:
            if name_src == folder_src:
                res.source, res.source_evidence = name_src, "both"
            else:
                # a specific file-name signature beats the folder label
                if rule["confidence"] == "high":
                    res.source, res.source_evidence = name_src, "conflict"
                else:
                    res.source, res.source_evidence = folder_src, "conflict"
                res.notes.append(f"folder says {folder_src}, name says {name_src}")
        elif name_src:
            res.source, res.source_evidence = name_src, "filename"
        elif folder_src:
            res.source, res.source_evidence = folder_src, "folder"
        res.loose = not res.in_source_folder

        # 4) stage / kind / confidence
        if rule:
            res.stage, res.kind, res.rule_id = rule["stage"], rule["kind"], rule["id"]
            res.confidence = rule["confidence"]
            if res.source_evidence == "folder" and res.confidence == "low":
                res.confidence = "medium"
        elif folder_src:
            res.stage, res.confidence = "RAW", "low"
            res.notes.append("no file-name rule; source from folder only")
        if any(norm(f).startswith(tuple(self.derived_folders)) for f in folders) \
                and res.stage in ("RAW", "EXTRACTED"):
            res.stage = "DERIVED"
            res.notes.append("inside a lab-derived folder")

        # 5) subject
        subj_idx = -1
        rxs = self.subject_rx.get(project, self.default_subject_rx)
        for i, f in enumerate(folders):
            if norm(f) in WRAPPERS or self.folder_source(f):
                continue
            for rx in rxs:
                sm = rx.match(f.strip())
                if sm:
                    res.subject = str(int(sm.group("subject")))
                    res.subject_evidence = "folder"
                    subj_idx = i
                    if "date" in sm.groupdict() and sm.group("date"):
                        res.session_date = res.session_date or parse_date(sm.group("date"))
                    if not res.session_date:
                        res.session_date = parse_date(f)
                    break
            if subj_idx >= 0:
                break

        # 5b) subject from the file name when no folder carries it
        if res.subject is None:
            stem = fname.rsplit(".", 1)[0]
            for rx in (x for x in rxs if x.pattern != r"^(?P<subject>\d{1,3})$"):
                sm = rx.match(stem.strip())
                if sm:
                    res.subject = str(int(sm.group("subject")))
                    res.subject_evidence = "filename"
                    res.session_date = res.session_date or parse_date(stem)
                    break

        # 6) session time from name (most precise) then from folders
        if rule and m and rule.get("session"):
            iso = session_from_match(rule["session"], m)
            if iso:
                res.session_start, res.session_date = iso, iso[:10]
        if not res.session_start:
            for f in reversed(folders):
                iso = session_from_folder(f)
                if iso:
                    res.session_start = iso if "T" in iso else None
                    res.session_date = iso[:10]
                    break

        # 7) proposed canonical location (NOT applied, only proposed)
        anchor = max(subj_idx, src_idx,
                     max((i for i, f in enumerate(folders) if norm(f) in WRAPPERS), default=-1))
        tail = folders[anchor + 1:] + [fname]
        layer = LAYER.get(res.stage)
        if layer and project:
            res.canonical_path = "/".join(
                [project, "Database", layer]
                + ([] if layer == "Metadata" else [res.subject or "_unassigned",
                                                   res.source or "_unknown"])
                + tail)
        if res.subject is None and res.stage not in ("METADATA", "IGNORE"):
            res.notes.append("subject not found in path")
        return res


# ----------------------------------------------------------------------
def parse_date(s: str) -> str | None:
    s = s.strip()
    for rx, fmt in ((r"(?<!\d)(20\d{2})[-_]?(\d{2})[-_]?(\d{2})(?!\d)", "ymd"),
                    (r"(?<!\d)(\d{2})-(\d{2})-(20\d{2})(?!\d)", "dmy")):
        mm = re.search(rx, s)
        if mm:
            a, b, c = mm.groups()
            y, mo, d = (a, b, c) if fmt == "ymd" else (c, b, a)
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                return f"{y}-{mo}-{d}"
    mm = re.search(r"(\d{1,2})\s+([a-zéû]+)\s+(20\d{2})", s.lower())
    if mm and mm.group(2) in MONTHS_FR:
        return f"{mm.group(3)}-{MONTHS_FR[mm.group(2)]:02d}-{int(mm.group(1)):02d}"
    return None


def session_from_match(recipe: str, m: re.Match) -> str | None:
    g = m.groupdict()
    try:
        if recipe == "ts" and g.get("ts"):
            t = g["ts"]
            return f"{t[:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}:{t[12:14]}"
        if recipe == "yymmddhhmmss" and g.get("yymmddhhmmss"):
            t = g["yymmddhhmmss"]
            return f"20{t[:2]}-{t[2:4]}-{t[4:6]}T{t[6:8]}:{t[8:10]}:{t[10:12]}"
        if recipe == "d_t" and g.get("d"):
            d, t = g["d"].replace("-", ""), g.get("t") or "0000"
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:00"
    except (TypeError, IndexError):
        return None
    return None


def session_from_folder(f: str) -> str | None:
    mm = re.match(r"^(\d{4}-\d{2}-\d{2})[ _](\d{2})-?(\d{2})(?:-(\d{2}))?", f)
    if mm:
        return f"{mm.group(1)}T{mm.group(2)}:{mm.group(3)}:{mm.group(4) or '00'}"
    return parse_date(f)


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inventory", help="CSV with a 'full_path' column (Azure) or 'dropbox_path'+'project'")
    ap.add_argument("--out", default="reports/phase1")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    c = Classifier()
    rows = list(csv.DictReader(open(a.inventory, encoding="utf-8-sig")))
    results = []
    for r in rows:
        if "full_path" in r:
            res = c.classify(r["full_path"])
        else:
            res = c.classify(r["relative_path"], project=r["project"])
        d = res.row()
        d["size_bytes"] = r.get("size_bytes") or r.get("size")
        results.append(d)
    fields = list(results[0].keys())
    with open(out / "classified_files.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)
    summarize(results, out)


def summarize(results: list[dict], out: Path):
    n = len(results)
    by = lambda k: Counter(r[k] for r in results)
    lines = [f"# Classification summary ({n} files)", ""]
    for k in ("stage", "source", "confidence", "source_evidence"):
        lines += [f"## {k}", "", "| value | files |", "|---|---:|"]
        lines += [f"| {v} | {c} |" for v, c in by(k).most_common()]
        lines.append("")
    loose = [r for r in results if r["loose"] and r["stage"] not in ("IGNORE",)]
    lines += [f"## Loose files (not in a source folder): {len(loose)}", "",
              "| source (from name) | files |", "|---|---:|"]
    lines += [f"| {v} | {c} |" for v, c in Counter(r["source"] for r in loose).most_common()]
    unres = [r for r in results if r["subject"] is None and r["stage"] not in ("IGNORE", "METADATA")]
    lines += ["", f"## Subject unresolved: {len(unres)}", ""]
    lines += [f"- `{r['path']}`" for r in unres[:60]]
    unk = [r for r in results if not r["source"] and r["stage"] not in ("IGNORE",)]
    lines += ["", f"## Source unknown: {len(unk)}", ""]
    lines += [f"- `{r['path']}`" for r in unk[:60]]
    (out / "classification_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:60]))


if __name__ == "__main__":
    main()
