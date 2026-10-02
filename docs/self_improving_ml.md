# Self-Improving ML Architecture
# K's vision (2026-10-02): the model searches for better methods itself
#
# The pipeline doesn't just process data — it evolves.

## Core Components

### 1. Self-Supervised Pretraining (SSL)
- **What**: Train on ALL unlabeled data with masked reconstruction
- **Why**: 2025 papers show 2.5% labeled data matches fully supervised
- **How**: Mask segments of ECG/PLETH/ABP, predict from context
- **Multi-modal**: Like SNUPHY-M — mask one signal, predict from others
- **Benefit**: Model learns from all 10 projects before K labels anything

### 2. Change Point Detection (PELT)
- **What**: Auto-detect regime shifts in time series
- **Library**: `ruptures` (Python) or `ruptures-rs` (Rust, 18-6000x faster)
- **Use**: Ventilator changes, drug administration, induction/emergence
- **Output**: Auto-generated event annotations for event-aligned analysis
- **Benefit**: No manual event labeling needed

### 3. SMoLK (Sparse Mixture of Learned Kernels)
- **What**: Interpretable lightweight neural architecture
- **Source**: Nature Machine Intelligence 2024 (Duke)
- **Why**: Matches 100x larger models, but interpretable (auditable)
- **Use**: Artifact detection, AF detection
- **Benefit**: Efficient + K can see what each kernel learned

### 4. Contrastive Learning
- **What**: Learn representations where clean ≠ artifact
- **Use**: Pretraining for artifact detection
- **Benefit**: Works with minimal labels

### 5. Granger Causality / Transfer Entropy
- **What**: Directional information flow between signals
- **Question**: Does NOL *predict* HR changes? (not just correlate)
- **Use**: Drug → response causal analysis
- **Benefit**: Beyond correlation to causation

### 6. Dynamic Time Warping (DTW)
- **What**: Align events across patients with different durations
- **Use**: Compare incision response in 2h vs 6h surgeries
- **Benefit**: Cross-patient comparison on warped time

### 7. Legendre Memory Unit (LMU)
- **What**: Brain-inspired state space model, O(N) scaling
- **Why**: Transformers are O(N²) — too slow for long recordings
- **Use**: Long-term temporal modeling
- **Benefit**: Efficient streaming, low memory

## The Self-Improving Loop

```
┌─────────────────────────────────────────┐
│         METHOD CHALLENGER               │
│                                         │
│  Current champion method                │
│       vs                                │
│  New candidate methods                  │
│       │                                 │
│       ▼                                 │
│  ┌─────────────┐                        │
│  │  BENCHMARK  │                        │
│  │  HARNESS    │                        │
│  │             │                        │
│  │ Fixed val   │                        │
│  │ set + metrics│                       │
│  └─────────────┘                        │
│       │                                 │
│       ▼                                 │
│  Winner → Production                    │
│  Loser → Archive (with reason)          │
└─────────────────────────────────────────┘
```

### Components:

1. **Benchmark Harness**
   - Fixed held-out validation set (never changes)
   - Standard metrics: F1, AUROC, precision/recall for artifact detection
   - Every method tested identically

2. **Optuna Hyperparameter Search**
   - Automatic tuning with early pruning (kills bad trials fast)
   - Efficient: doesn't waste compute on losers

3. **TPOT-Style Evolutionary Search**
   - Genetic programming discovers novel pipeline combinations
   - Search once, export efficient code
   - Human might never try these combinations

4. **Literature Monitor** (scheduled monthly)
   - Searches for new papers in physiological signal processing
   - Flags promising methods for benchmark testing
   - Creates GitHub issue: "New method X to test"

5. **Monthly Evolution Report**
   ```
   Tested 12 new approaches this month
   2 beat the current champion:
     - SMoLK for ECG artifact: +3.2% F1
     - TV denoising for drug steps: +1.8% precision
   Promoted to production.
   ```

## Implementation Phases

### Phase A (now): Foundation
- [ ] SSL pretraining on ingested data
- [ ] PELT change point detection for auto-events
- [ ] Benchmark harness with fixed validation set

### Phase B (after Phase A works)
- [ ] SMoLK for artifact detection
- [ ] Contrastive pretraining
- [ ] Optuna hyperparameter search

### Phase C (mature system)
- [ ] TPOT evolutionary pipeline search
- [ ] Granger causality analysis
- [ ] DTW for cross-patient alignment
- [ ] Monthly literature monitor + evolution reports

## Key Principles

1. **Champion/Challenger**: Never replace working method without beating it on validation
2. **Auditability**: Every promotion logged with metrics, K can review
3. **Efficiency**: Prune bad trials early, search once, deploy efficient code
4. **Interpretability**: Prefer methods K can understand (SMoLK > black box)
5. **All inside Azure**: No external APIs, no data leaves perimeter
