"""Head-to-head model comparison for artifact detection.

Trains and evaluates on the SAME stratified test split:
  - rf:        RandomForest on 20 hand-crafted features (current baseline)
  - cnn:       1D CNN on raw 100-sample windows (Howard et al. 2019 style)
  - trans:     SignalTransformer trained from scratch on labels only
  - ssl_trans: SignalTransformer pretrained (contrastive) then fine-tuned

Metrics per model: accuracy, F1, AUROC, MCC, sensitivity, specificity.
Focus signals: ART / RAD / BRA (the broken RF models).

Usage:
  python -m tools.compare_models --parquet-dir processed_dl --out ml_out/compare \\
      --signal ART --labels-csv labels.csv --max-files 200
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch.utils.data import Dataset, DataLoader
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

from tools.ssl_transformer import (
    SignalTransformer, ContrastiveDataset, LabeledDataset,
    collect_windows, smooth_infonce, two_views,
    WINDOW, D_MODEL, N_LAYERS, N_HEADS, D_FF, DROPOUT, PROJ_DIM,
)


# ----------------------------------------------------------------------------
# 1D CNN (beat/window classifier, Howard et al. 2019 style)
# ----------------------------------------------------------------------------

class WindowCNN(nn.Module):
    def __init__(self, dropout: float = 0.3):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(1, 32, kernel_size=7, padding=3), nn.ReLU(),
            nn.BatchNorm1d(32), nn.MaxPool1d(2), nn.Dropout(dropout),
            nn.Conv1d(32, 64, kernel_size=5, padding=2), nn.ReLU(),
            nn.BatchNorm1d(64), nn.MaxPool1d(2), nn.Dropout(dropout),
            nn.Conv1d(64, 128, kernel_size=3, padding=1), nn.ReLU(),
            nn.BatchNorm1d(128), nn.AdaptiveAvgPool1d(1),
        )
        self.fc = nn.Sequential(
            nn.Linear(128, 64), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(64, 2))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, T] -> [B, 1, T]
        h = self.net(x.unsqueeze(1)).squeeze(-1)  # [B, 128]
        return self.fc(h)


def train_torch_classifier(model, tr_dl, va_dl, device, epochs=15,
                           lr=1e-3, name="model"):
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    ce = nn.CrossEntropyLoss()
    best_va, best_sd = -1.0, None
    for ep in range(epochs):
        model.train()
        for xb, yb in tr_dl:
            xb, yb = xb.to(device), yb.to(device)
            loss = ce(model(xb), yb)
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        # val acc
        model.eval()
        correct, total = 0, 0
        with torch.no_grad():
            for xb, yb in va_dl:
                pred = model(xb.to(device)).argmax(1).cpu()
                correct += (pred == yb).sum().item()
                total += len(yb)
        va_acc = correct / max(total, 1)
        if (ep + 1) % 5 == 0 or ep == 0:
            print(f"[compare:{name}] epoch {ep+1}/{epochs} va_acc {va_acc:.3f}",
                  flush=True)
        if va_acc > best_va:
            best_va = va_acc
            best_sd = {k: v.cpu() for k, v in model.state_dict().items()}
    model.load_state_dict(best_sd)
    return model


def evaluate_torch(model, te_dl, device, forward_fn=None):
    from sklearn.metrics import accuracy_score, f1_score, roc_auc_score, \
        matthews_corrcoef
    model.eval()
    P, L, S = [], [], []
    with torch.no_grad():
        for xb, yb in te_dl:
            xb = xb.to(device)
            logits = forward_fn(model, xb) if forward_fn else model(xb)
            prob = torch.softmax(logits, dim=1)[:, 1]
            P.extend(prob.cpu().numpy())
            L.extend(yb.numpy())
            S.extend((prob > 0.5).long().cpu().numpy())
    P, L, S = map(np.array, (P, L, S))
    return {
        "accuracy": float(accuracy_score(L, S)),
        "f1": float(f1_score(L, S, zero_division=0)),
        "auroc": float(roc_auc_score(L, P)) if len(np.unique(L)) > 1 else 0.0,
        "mcc": float(matthews_corrcoef(L, S)),
        "sensitivity": float(S[L == 1].mean()) if (L == 1).any() else 0.0,
        "specificity": float(1 - S[L == 0].mean()) if (L == 0).any() else 0.0,
    }


def trans_forward(model, xb):
    return model.forward_classify(xb)


# ----------------------------------------------------------------------------
# RF baseline (reuses ml_supervised_train feature extraction)
# ----------------------------------------------------------------------------

def rf_features_for_windows(W: np.ndarray, signal_name: str = "") -> np.ndarray:
    from tools.ml_supervised_train import is_art_signal, \
        extract_art_morphology_features
    feats = []
    for w in W:
        mask = ~np.isnan(w)
        wv = w[mask]
        if len(wv) < 10:
            feats.append([0.0] * 20)
            continue
        diffs = np.abs(np.diff(wv)) if len(wv) > 1 else np.array([0])
        base = [np.mean(wv), np.std(wv), np.min(wv), np.max(wv),
                np.max(wv) - np.min(wv), np.median(wv),
                np.max(diffs), len(np.unique(np.round(wv, 2)))]
        if is_art_signal(signal_name):
            feats.append(base + extract_art_morphology_features(wv))
        else:
            feats.append(base + [0.0] * 12)
    return np.array(feats)


def train_rf(Xtr, ytr, Xva, yva, Xte, yte):
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import accuracy_score, f1_score, roc_auc_score, \
        matthews_corrcoef
    m = RandomForestClassifier(n_estimators=100, max_depth=12,
                               random_state=42, n_jobs=-1)
    m.fit(Xtr, ytr)
    p = m.predict(Xte)
    pr = m.predict_proba(Xte)[:, 1]
    return {
        "accuracy": float(accuracy_score(yte, p)),
        "f1": float(f1_score(yte, p, zero_division=0)),
        "auroc": float(roc_auc_score(yte, pr)) if len(np.unique(yte)) > 1 else 0.0,
        "mcc": float(matthews_corrcoef(yte, p)),
        "sensitivity": float(p[yte == 1].mean()) if (yte == 1).any() else 0.0,
        "specificity": float(1 - p[yte == 0].mean()) if (yte == 0).any() else 0.0,
    }


# ----------------------------------------------------------------------------
# Main comparison
# ----------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--signal", default="ART")
    ap.add_argument("--labels-csv", default=None)
    ap.add_argument("--max-files", type=int, default=200)
    ap.add_argument("--max-windows", type=int, default=50000)
    ap.add_argument("--pretrain-windows", type=int, default=100000,
                    help="Unlabeled windows for SSL pretraining")
    ap.add_argument("--pretrain-epochs", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    if not HAS_TORCH:
        print("[compare] PyTorch required", flush=True)
        return 1
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    sig = None if args.signal.lower() == "all" else args.signal
    pdir = Path(args.parquet_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device = args.device

    # ---- shared labeled data, one stratified split for ALL models ----
    W, Y = collect_windows(pdir, sig, max_files=args.max_files,
                           max_windows=args.max_windows,
                           labels_csv=args.labels_csv, need_labels=True)
    if Y is None or len(np.unique(Y)) < 2:
        print("[compare] need both classes", flush=True)
        return 1
    from sklearn.model_selection import train_test_split
    idx = np.arange(len(W))
    i_tr, i_te = train_test_split(idx, test_size=0.15, random_state=args.seed,
                                  stratify=Y)
    i_tr2, i_va = train_test_split(i_tr, test_size=0.176,
                                   random_state=args.seed, stratify=Y[i_tr])
    print(f"[compare] split: train {len(i_tr2)} val {len(i_va)} "
          f"test {len(i_te)} (artifact {Y.mean():.1%})", flush=True)

    results = {}
    B = args.batch_size

    # ---- 1. RF baseline ----
    print("[compare] training RF baseline ...", flush=True)
    F_all = rf_features_for_windows(W, args.signal)
    results["rf"] = train_rf(F_all[i_tr2], Y[i_tr2], F_all[i_va], Y[i_va],
                             F_all[i_te], Y[i_te])
    print(f"[compare] RF test F1 {results['rf']['f1']:.3f} "
          f"AUROC {results['rf']['auroc']:.3f}", flush=True)

    # ---- torch dataloaders (shared) ----
    tr_dl = DataLoader(LabeledDataset(W[i_tr2], Y[i_tr2]), batch_size=B,
                       shuffle=True)
    va_dl = DataLoader(LabeledDataset(W[i_va], Y[i_va]), batch_size=B)
    te_dl = DataLoader(LabeledDataset(W[i_te], Y[i_te]), batch_size=B)

    # ---- 2. 1D CNN ----
    print("[compare] training 1D CNN ...", flush=True)
    cnn = WindowCNN().to(device)
    cnn = train_torch_classifier(cnn, tr_dl, va_dl, device,
                                 epochs=args.epochs, name="cnn")
    results["cnn"] = evaluate_torch(cnn, te_dl, device)
    print(f"[compare] CNN test F1 {results['cnn']['f1']:.3f} "
          f"AUROC {results['cnn']['auroc']:.3f}", flush=True)

    # ---- 3. Transformer from scratch ----
    print("[compare] training transformer (from scratch) ...", flush=True)
    trans = SignalTransformer().to(device)
    trans = train_torch_classifier(
        trans, tr_dl, va_dl, device, epochs=args.epochs, name="trans")
    results["transformer_scratch"] = evaluate_torch(
        trans, te_dl, device, forward_fn=trans_forward)
    print(f"[compare] trans test F1 {results['transformer_scratch']['f1']:.3f} "
          f"AUROC {results['transformer_scratch']['auroc']:.3f}", flush=True)

    # ---- 4. SSL-Transformer: pretrain then fine-tune ----
    print("[compare] SSL pretraining ...", flush=True)
    Wu, _ = collect_windows(pdir, sig, max_files=args.max_files,
                            max_windows=args.pretrain_windows)
    uds = ContrastiveDataset(Wu, seed=args.seed)
    udl = DataLoader(uds, batch_size=B, shuffle=True, drop_last=True)
    ssl = SignalTransformer().to(device)
    opt = torch.optim.AdamW(ssl.parameters(), lr=3e-4, weight_decay=1e-4)
    ssl.train()
    for ep in range(args.pretrain_epochs):
        tot, n = 0.0, 0
        for v1, v2 in udl:
            v1, v2 = v1.to(device), v2.to(device)
            loss = smooth_infonce(ssl.forward_pretrain(v1),
                                  ssl.forward_pretrain(v2))
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(ssl.parameters(), 1.0)
            opt.step()
            tot += loss.item()
            n += 1
        print(f"[compare:ssl-pretrain] epoch {ep+1}/{args.pretrain_epochs} "
              f"loss {tot/max(n,1):.4f}", flush=True)
    print("[compare] fine-tuning SSL transformer ...", flush=True)
    # Linear probe 3 epochs then full fine-tune
    ssl = train_torch_classifier(ssl, tr_dl, va_dl, device,
                                 epochs=args.epochs, name="ssl_trans")
    results["ssl_transformer"] = evaluate_torch(
        ssl, te_dl, device, forward_fn=trans_forward)
    print(f"[compare] SSL test F1 {results['ssl_transformer']['f1']:.3f} "
          f"AUROC {results['ssl_transformer']['auroc']:.3f}", flush=True)

    # ---- report ----
    report = {
        "signal": args.signal,
        "n_windows": len(W),
        "artifact_frac": float(Y.mean()),
        "split": "70/15/15 stratified, seed 42 (identical for all models)",
        "results": results,
    }
    (out / "comparison_report.json").write_text(json.dumps(report, indent=1))

    print("\n=========== COMPARISON (test set) ===========", flush=True)
    print(f"{'model':<20} {'acc':>6} {'f1':>6} {'auroc':>6} {'mcc':>6} "
          f"{'sens':>6} {'spec':>6}", flush=True)
    for name in ["rf", "cnn", "transformer_scratch", "ssl_transformer"]:
        r = results[name]
        print(f"{name:<20} {r['accuracy']:>6.3f} {r['f1']:>6.3f} "
              f"{r['auroc']:>6.3f} {r['mcc']:>6.3f} "
              f"{r['sensitivity']:>6.3f} {r['specificity']:>6.3f}", flush=True)
    best = max(results, key=lambda k: results[k]["f1"])
    print(f"\nBest by F1: {best} ({results[best]['f1']:.3f})", flush=True)
    # Save the winning SSL model for smart_filter use
    if best == "ssl_transformer":
        torch.save({"state_dict": {k: v.cpu() for k, v in ssl.state_dict().items()},
                    "config": {"d_model": D_MODEL, "n_layers": N_LAYERS,
                               "n_heads": N_HEADS, "d_ff": D_FF,
                               "dropout": DROPOUT, "proj_dim": PROJ_DIM,
                               "window": WINDOW, "step": 50},
                    "signal": args.signal, "metrics": results[best]},
                   out / f"{args.signal}_ssl.pt")
        print(f"[compare] saved winning SSL model", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
