# NOL / Medasense PMD-200 companion-file format research

**Date:** 2026-10-01
**Scope:** file formats found alongside the NOL `ExcelData.csv` export in patient folders:
`.pmd`, `.med`, `metadata.json`, `PMD_LOG.csv`, `Patient.mat`, `Processing.mat`,
`chan.mat`, `testinfo.mat`, and `.enc` variants.
**Method:** web search + vendor manual + peer-reviewed literature. No files downloaded,
nothing executed.

## Background (CONFIRMED)

- The PMD-200 (Medasense Biometrics Ltd., Ramat Gan, Israel) computes the NOL
  (Nociception Level) index: a random-forest multiparametric index, 0–100,
  updated every 5 s, from a finger probe with four sensors — photoplethysmography
  (PPG), galvanic skin response (GSR/skin conductance), peripheral temperature,
  and accelerometry. Derived parameters include PPG wave amplitude (PPGA), HRV
  high-frequency band power, number of skin-conductance fluctuations (NSCF), skin
  conductance level, HR, and their time derivatives.
  (F1000Research 2018;7:875; MDPI Medicina 2024;60:1921; encyclopedia.pub review.)
- Core IP: US Patent 8,512,240, "System and Method for Pain Monitoring Using a
  Multidimensional Analysis of Physiological Signals" (Medasense).
- The vendor user manual (MK2L-02-487-BR, Rev 2, May 2019 — the PT-BR edition
  hosted by a Brazilian distributor) documents exactly two export options,
  available only when no monitoring session is running:
  1. **"Exportar pasta para USB"** — exports *all raw data* of a monitoring
     session to USB ("todos os dados não processados");
  2. **"Exportar arquivo Excel para USB"** — exports an Excel file with
     time, NOL index, heart rate, and annotations; also saved "na pasta do
     paciente" (in the patient folder).
- Recordings are listed on the device by start timestamp (`AAAA-MM-DD hhmm`).
- The manual contains **no mention of encryption** (searched for
  "encrypt"/"criptograf" — zero hits) and **no description of any raw-export
  file layout**. The technical/service mode is password-protected and reserved
  for Medasense personnel.
- No open-source reader/parser for any PMD-200 export format was found
  (GitHub/code searches returned only unrelated "PMD" software-project hits).

**Implication:** the companion files almost certainly come from export option 1
(raw session folder). Only `ExcelData.csv` (option 2) has a layout the pipeline
already understands. Everything below is therefore vendor-proprietary or
site-specific.

---

## 1. `.pmd` files

- **What it likely is (SPECULATIVE):** the raw session data file written by the
  "export folder" option — plausibly "Pain Monitoring Data". No vendor manual,
  paper, patent, or public code documents this extension for Medasense.
- **Evidence level:** zero public documentation found.
- **How to proceed:** structural forensics only (magic bytes, entropy profile,
  printable-string scan, size-vs-session-duration ratio) on real files; never
  guess field layouts.
- **Verdict: QUARANTINE** — unknown proprietary format. Keep in review queue;
  candidate for a vendor data-dictionary request (Medasense supplies research
  sites under agreement; the manual's technical mode is vendor-only).

## 2. `.med` files

- **What it likely is (SPECULATIVE):** another Medasense session/data file from
  the raw folder export. No public documentation connects `.med` to Medasense;
  the extension is used by unrelated software elsewhere, so name collisions are
  possible.
- **Evidence level:** zero public documentation found.
- **How to proceed:** same structural forensics as `.pmd`; check whether sizes
  correlate with session duration (time series) or are small/fixed (config).
- **Verdict: QUARANTINE** — unknown. Keep in review queue.

## 3. `metadata.json`

- **What it likely is (SPECULATIVE):** session/device metadata written by the
  raw folder export. Plausible fields, inferred from what the device knows
  (manual: patient details can be entered per session; the About screen shows
  SW/FW versions; recordings are timestamped): session start datetime, device
  serial number, software/firmware versions, recording duration, patient
  demographics as entered on the device, probe/sensor identifiers.
- **Evidence level:** no public schema found. The field list above is inference,
  not documentation.
- **How to proceed:** JSON is safely parseable — extract the key inventory and
  value *types/shapes* only; treat every field's meaning as unverified until
  cross-checked against the device UI labels in the manual. Flag any
  free-text/identifier-looking values through the identifier review process
  before they go anywhere near a feed.
- **Verdict: PARSEABLE (container)** — JSON syntax is standard; field semantics
  are UNVERIFIED. Parse structurally, quarantine interpretation.

## 4. `PMD_LOG.csv`

- **What it likely is (SPECULATIVE):** a device operation/event log. The name
  suggests log rows with timestamps; plausible event types from the manual's
  UI flows: session start/stop, probe connect/disconnect, signal-quality or
  artifact alarms, annotation events, export/delete actions, system errors,
  low-memory warnings.
- **Evidence level:** no public documentation of columns or event codes found.
- **How to proceed:** parse as CSV (header + delimiter sniffing, same as the
  existing CSV census tooling); inventory columns and the distinct event-code
  vocabulary; map event codes to manual UI strings where possible; everything
  else stays in the review queue.
- **Verdict: PARSEABLE (container)** — CSV syntax is standard; column/event
  semantics are UNVERIFIED. Parse structurally, quarantine interpretation.

## 5. `.mat` files (`Patient.mat`, `Processing.mat`, `chan.mat`, `testinfo.mat`)

- **What they are (CONFIRMED, container-level):** MATLAB data files. Readable
  with `scipy.io.loadmat` (MATLAB v5/v7) or `h5py` (v7.3, which is HDF5-based);
  format auto-detection is standard practice.
- **What they likely contain (SPECULATIVE):** the variable/file names have
  **zero** public documentation in connection with Medasense and do not appear
  in the vendor manual. `Patient` / `Processing` / `chan` (channels?) /
  `testinfo` read like a *hospital/research-team MATLAB pipeline's* naming, not
  a device export convention — i.e. these may be intermediate products of the
  lab's own earlier analysis rather than PMD-200 output. This is inference
  from naming only.
- **Evidence level:** container format confirmed; contents undocumented.
- **How to proceed:** open read-only, inventory variable names, classes, and
  dimensions (never values beyond shape/dtype); check for timestamps or
  sampling-rate variables that would allow time alignment; do not assume
  physiological meaning for any variable.
- **Verdict: PARSEABLE (container)** — MATLAB v5/v7/v7.3 readable via
  scipy/h5py; variable semantics are UNVERIFIED and possibly site-specific.
  Parse structurally, quarantine interpretation.

## 6. `.enc` versions

- **Does Medasense encrypt these? (CONFIRMED negative):** the user manual
  contains no mention of encryption anywhere in the export, data-management,
  or technical sections. No paper, press release, or patent found describes
  encrypted PMD-200 exports. The USB export flow described is plaintext file
  copy.
- **What `.enc` likely is (SPECULATIVE):** encryption applied *after* export by
  the hospital/research team (or an archiving tool) — not a Medasense format.
- **Decryption path:** none legitimate without the key holder. Do not attempt
  to break it; ask the data owner (the lab member who created the archive)
  for the tool/password. If the key is lost, the files stay encrypted in
  Dropbox (ground truth) and out of Azure — same rule as other unsupported
  bytes, except these are *supported formats with an access problem*, so they
  get their own review-queue category rather than being treated as junk.
- **Verdict: ENCRYPTED** — no legitimate decryption path from the pipeline's
  side. Quarantine separately; resolve via the data owner.

## 7. Open-source readers/parsers

- **Finding (CONFIRMED):** none exist for any PMD-200 export format. Searches
  across GitHub and the web surfaced no `.pmd`/`.med`/PMD_LOG/metadata.json
  readers, no Medasense SDK, and no community reverse-engineering write-ups.
  (The only GitHub hit was an unrelated "clinical indices" notes repo that
  describes the NOL index conceptually.)
- The Philips Capsule integration (2026) streams NOL data to EHRs — a future
  *live* data path, not a file-format reader, and not applicable to this
  archive.
- **Verdict: QUARANTINE (ecosystem)** — no community tooling to lean on;
  parsers must be built in-house from structural forensics + vendor inquiry.

---

## Recommended next steps (for the parent agent)

1. **Structural forensics pass** over real `.pmd`/`.med` files (magic bytes,
   entropy, strings, size-vs-duration correlation) — same read-only,
   privacy-safe approach as the CSV forensics already built.
2. **Key/type inventory** of `metadata.json`, `PMD_LOG.csv` columns, and `.mat`
   variable tables — no values, shapes only.
3. **Ask Medasense** (via the lab's device contact) for the raw-export data
   dictionary; research sites routinely get this under agreement.
4. **Ask the lab** who created the `.enc` files and with what tool/password.
5. Keep everything quarantined in the review queue until (1)–(4) resolve —
   per the standing rule, unknown formats are never parsed by guessing and
   never discarded.

## Sources

- PMD-200 User Manual MK2L-02-487-BR Rev 2 (PT-BR, May 2019), distributor PDF:
  https://jgmoriya.com.br/wp-content/uploads/MK2L-02-487-PT-BR-REV-2-PMD200-2.1.pdf
- F1000Research 2018;7:875 — NOL response study (PMD100/PMD-200 methods):
  https://f1000research.com/articles/7-875
- MDPI Medicina 2024;60:1921 — NOL-guided analgesia RCT (NOL every 5 s, 0–100):
  http://www.mdpi.com/1648-9144/60/12/1921
- Encyclopedia.pub entry 42050 — pediatric nociception review (NoL 4-parameter
  composition: PPGA, HRV-HF, NSCF): https://encyclopedia.pub/entry/42050
- Medasense US Patent 8,512,240 notice:
  https://www.meddeviceonline.com/doc/medasense-patent-approval-novel-pain-monitoring-system-0001
- Medasense–Philips Capsule EHR integration (2026):
  https://mailchi.mp/medasense.com/nol-pmd-200-philips-capsule-integration
