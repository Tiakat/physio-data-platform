# K's Rules — Must Respect Always

## Scope
- Full scope: ALL 10 projects, ALL files, ALL physiological columns, EACH patient
- Never quietly narrow scope or call file slots "patients"
- Report real denominators: total Dropbox files vs eligible/ingested vs encrypted inputs vs linked patients vs graphs

## Data
- Dropbox is read-only ground truth: never move, rename, or reorganize it
- Missing values are NOT zero
- Pumps are exposure/event variables; NOL and BIS are indices (not signals to filter like vitals)
- BetterCare time = relative milliseconds; Infinity = absolute timestamps

## Azure Structure (4 containers ONLY)
1. **1-raw** — raw data as CSVs: `1-raw/{PROJECT}/{patient}/{device}/file.csv`
2. **2-processed** — filtered data as CSVs: same structure
3. **3-graphes** — PNGs: `3-graphes/{PROJECT}/{patient}/{column}.png`
4. **4-analysis** — stats: PNGs and Excel, same structure

- NOTHING else in Azure. No rawdata, no processed (old), no graphs, no reports, no $logs
- Extra metadata (ingest state, etc.) goes SOMEWHERE ELSE, not in these 4

## Patient Names
- Use EXACT Dropbox folder names (e.g., "103", not "patient_1" or "patient 103")
- Never create provisional numbers
- Weekly ingest compares Dropbox vs 1-raw by EXACT patient name match to find missing ones
- If names don't match exactly, incremental ingest breaks

## Files
- CSVs for now (clickable preview in browser, content_type=text/csv)
- After K verifies results: convert back to parquet
- Device folders: infinity, bettercare, bis, nol, pumps, etc. based on source
- BIS: convert to CSV (parse .r2a/.spa → CSV)

## Filtering
- Must be signal-specific, transparent, auditable (never generic)
- Preserve raw + filtered side by side
- SpO2 flat at 98-99% = NORMAL, never filter
- NBP gaps = NORMAL (cuff), never interpolate
- Safety rails: never remove 100%, flag for review at >50%

## Pipeline Chain (automatic, incremental)
Dropbox → 1-raw → 2-processed → 3-graphes → 4-analysis
- Each stage checks previous for missing patients (by exact name)
- Only processes what's missing
- Linked as one continuous chain with supervisors

## Supervisors
- **One supervisor per step**: monitors its own stage (ingest, process, graphs, analysis)
- **Continuity checkers between steps**: verify handoff (e.g., 1-raw → 2-processed: did all patients transfer? any missing? any corrupted?)
- **Meta-supervisor (general)**: oversees all supervisors, reports overall health, alerts on failures
- Supervisors must be linked as one chain, not isolated
- Each supervisor: done or not done must be explicit

## Graphs
- Every non-empty column gets a graph
- Directly browseable in Azure (content_type=image/png), no downloading
- Show: raw (gray) + filtered (red) + flagged (black dots)

## ML
- Fix supervised AND unsupervised (both broken)
- Focus on self-supervised transformers
- Train on ALL data, compare approaches
- ART/RAD/BRA models are priority (currently 0.56-0.68, useless)

## Infrastructure
- Lab pays cloud costs directly; K never fronts costs
- Keep supervisors (code in GitHub)

## Process
- K reviews pilot before full rollout (see → adapt → repeat)
- Proactive updates when milestones hit
- Tell her when each project/stage is done
- Never claim "done 100%" unless verified with proof
