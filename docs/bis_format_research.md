# BIS Vista export format research — 2026-10-01

Source: research subagent (web + literature), findings delivered 2026-10-01 20:21 UTC.
Status: `.r2a` and `.spa` CONFIRMED from peer-reviewed sources. Seven other
extensions UNKNOWN — zero public documentation. Verify against real files
before writing parsers for them.

## `.r2a` = raw EEG [CONFIRMED]

- Connor (2022), "Emulation of the BIS Engine", open access, PMC8449791, Table 3.
- Layout: channel-interleaved **signed 16-bit little-endian**, 2 channels, **128 Hz**
  (hardware decimates a 16384 Hz delta-sigma stream 128-fold), **no header**.
- µV scaling: `× 1675.42688 / 32767` (≈0.0511 µV/step, ±1675.4 µV full scale).
- Size check: 512 bytes/sec (~1.76 MB/hour).
- Open-source Python reference: `ettiennecoetzee/bis-vista-eeg-scope` (MIT, 2025)
  implements exactly the Connor layout — adapt for `tools/process_bis.py`
  / replace the `adapters/intraop_eeg.py` stub.

## `.spa` = 1 Hz parameter trends, plaintext [CONFIRMED]

- Pipe-delimited, 2 header lines, one row/sec, 54 columns.
- 0-based indices: timestamp 0, BIS triplet 11/25/39, SEF 36, MF 37, BSR 35,
  POW 42, EMG 43, suppression time 48.
- **Negative values = missing → NaN.**
- Two clinical papers corroborate (".r2a (BIS, sample rate 128 Hz)", ".spa (trends, 1 Hz)").
- Parser must be **header-name-driven** with Connor indices as fallback
  (layouts drift between firmware versions); multi-format timestamp parsing.

## `.ara`, `.e_a`, `.f_a`, `.h_a`, `.m_a`, `.o_a`, `.t_a` — [UNKNOWN]

- Not in Connor's study structure, not in the operator manual (lists export
  *types* but never extensions), not in the OSS tool.
- Educated guess: "bis_lfile" ≈ "Live-file" — may be the full file set of a
  BIS Vista **Live Data** USB export. Verify from real files.
- Keep quarantined in the review queue; never force-fit.

## No encryption, no proprietary blocker

- Formats officially undocumented (Aspect: "contact Technical Service") but
  unencrypted and partially published open-access. No external API needed.

## Recommended parser plan

1. `.r2a`: `<i2` interleave → reshape (derive channel count from file size,
   don't hardcode 2) → µV scale → 128 Hz timebase; verify endianness on first
   file (wrong choice = obvious full-scale noise).
2. `.spa`: header-name-driven mapping, negatives → NaN, multi-format timestamps.
3. Pair by filename stem; QC: r2a-seconds vs spa-rows reconciliation, flatline
   detection (BISx does periodic ground-impedance checks — flatline there is
   device artifact, not suppression; cross-check BSR first).
4. 7-step reverse-engineering plan for unknown extensions: inventory →
   text/binary probe → header hexdump → try known parsers → int16 plausibility
   vs same-case .r2a → rate detection → PDF cross-check.
5. Record everything in `configs/signals/bis.yaml` (status: draft) for K's
   interval review before any filtering goes live.

## Counts (from format/layout recon 2026-10-01)

BIS binaries: COLECTOMIE 590, POSBRAIN 159, PVB-ABDO 94, MONREPI 80.
