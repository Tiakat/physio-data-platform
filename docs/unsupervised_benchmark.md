# Unsupervised Method Benchmark — Compare All, Keep the Winner
# K's approach (2026-10-02): run everything, compare objectively, keep searching for new methods
#
# Like the supervised challenger loop, but for unsupervised phenotyping.

## Methods to Benchmark

### Classical
1. **K-means (Euclidean)** — baseline, stable but poor for non-spherical
2. **K-means (DTW distance)** — time-series aware
3. **K-Shape** — shape-based, shift/scale invariant (important for time series)
4. **HDBSCAN** — density-based, handles noise, but may over-classify as noise
5. **Spectral clustering** — non-spherical clusters, doesn't scale past ~30k
6. **Gaussian Mixture Models** — probabilistic, soft assignments
7. **Agglomerative (Ward)** — hierarchical, dendrogram for K selection

### Deep Learning
7. **SOM-VAE** — VAE + self-organizing map, interpretable prototypes
8. **T-DPSOM** — VAE + LSTM + probabilistic SOM, trajectory maps with uncertainty
9. **Deep Temporal K-means** — clustering in RNN embedding space
10. **Autoencoder + K-means** — simple deep baseline

### Interpretable
11. **Shapelets + K-means** — representative subsequences per cluster
12. **Time2Feat + clustering** — interpretable domain features first

### Hybrid
13. **Hybrid distance K-means** — non-Euclidean + refinement step

## Evaluation Metrics

Since there's no ground truth (unsupervised), use:

### Internal (no labels needed)
- **Silhouette score** — cluster cohesion vs separation
- **Davies-Bouldin index** — lower = better separated
- **Calinski-Harabasz** — variance ratio
- **DTW-based silhouette** — using DTW distance instead of Euclidean

### Stability
- **Bootstrap stability** — do clusters persist across resamples?
- **Cross-project generalization** — train on DEXREM, test on PROMISES

### Clinical Relevance (K judges)
- **Phenotype interpretability** — can K describe each cluster?
- **Event alignment** — do clusters correspond to meaningful patterns?
- **Actionability** — would this change clinical understanding?

### Efficiency
- **Runtime** — wall clock on 100 patients
- **Memory** — peak RAM
- **Scalability** — how does it scale to 1000 patients?

## Benchmark Protocol

```
For each method:
  1. Run on VitalDB test set (public, fast iteration)
  2. Compute all internal metrics
  3. Test cross-project generalization
  4. Measure runtime/memory
  5. Generate cluster visualizations
  
Rank by EFFICIENCY-WEIGHTED composite score:
  score = (performance × stability) / log(compute_cost)
  
A method 2% better but 100x slower LOSES.
Efficiency is a first-class criterion, not a tiebreaker.
```

Winner → production unsupervised pipeline
Top 3 → kept as challengers for future comparisons
All results → logged with full reproducibility

## Continuous Search (Muse's Job)

Monthly, I search for:
- New clustering papers (arXiv, Nature MI, IEEE TBME)
- New time-series representation methods
- New benchmark results

Promising methods → added to benchmark → compared → promoted if better.

K gets a monthly report:
```
Unsupervised benchmark update:
  Tested: 3 new methods (X, Y, Z)
  Winner still: T-DPSOM (silhouette 0.42)
  Challenger Y close second (0.39) — worth watching
  No promotion this month.
```

## Implementation

Phase 1: Classical methods (fast, establish baseline)
Phase 2: Deep methods (need more compute, better results expected)
Phase 3: Continuous monthly search + benchmark
