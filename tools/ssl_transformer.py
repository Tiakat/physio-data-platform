"""Self-supervised transformer for physiological signal artifact detection.

Two-stage approach (Le et al. 2025, CHU Sainte-Justine):
  1. PRETRAIN: contrastive learning (Smooth InfoNCE) on ALL unlabeled
     signal windows. Augmentations create two views of each window;
     the transformer learns representations that are invariant to the
     augmentations but distinct across different windows.
  2. FINE-TUNE: attach a classification head and train on labeled
     windows (PROMISES labels, pseudo-labels) to detect artifacts.

Architecture:
  - Input: 100-sample windows (matches RF pipeline for drop-in use)
  - Linear(1 -> d_model=128) input projection + sinusoidal positional encoding
  - TransformerEncoder: 4 layers, 8 heads, dim_feedforward=256
  - Pretrain head: projection to 64-dim contrastive space
  - Classify head: mean-pool -> Linear(d_model -> 2)

Usage:
  python -m tools.ssl_transformer --mode pretrain --parquet-dir processed_dl \\
      --out ml_out/ssl --signal ART --max-files 100 --epochs 10
  python -m tools.ssl_transformer --mode finetune --parquet-dir processed_dl \\
      --out ml_out/ssl --signal ART --pretrained ml_out/ssl/ART_encoder.pt \\
      --labels-csv labels.csv
  python -m tools.ssl_transformer --mode predict --parquet-dir processed_dl \\
      --model ml_out/ssl/ART_ssl.pt --signal ART --out preds.json
"""

from __future__ import annotations

import argparse
import io
import json
import math
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

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

WINDOW = 100          # samples per window (matches RF pipeline)
STEP = 50             # sliding step
D_MODEL = 128
N_LAYERS = 4
N_HEADS = 8
D_FF = 256
DROPOUT = 0.1
PROJ_DIM = 64         # contrastive projection dim
TEMPERATURE = 0.1     # InfoNCE temperature
LABEL_SMOOTH = 0.05   # smooth InfoNCE label smoothing


# ----------------------------------------------------------------------------
# Data utilities (shared patterns with ml_supervised_train)
# ----------------------------------------------------------------------------

def read_parquet_any(path) -> pd.DataFrame:
    data = Path(path).read_bytes()
    if str(path).endswith(".enc"):
        from tools.crypto import decrypt_bytes
        data = decrypt_bytes(data)
    return pd.read_parquet(io.BytesIO(data))


def sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_"
                   for c in name)


def signal_columns(df: pd.DataFrame) -> list:
    cols = []
    for col in df.columns:
        low = col.lower()
        if low in ("timestamp", "time", "t") or low.endswith("__qc") \
                or low.startswith("_"):
            continue
        cols.append(col)
    return cols


def patient_from_path(path: Path) -> str | None:
    import re
    m = re.search(r'patient\s*(\d+)', str(path).lower())
    return f"patient {m.group(1)}" if m else None


def project_from_path(path: Path) -> str | None:
    parts = Path(path).parts
    for i, p in enumerate(parts):
        if p == 'level2' and i + 1 < len(parts):
            return parts[i + 1].upper()
    return None


def extract_windows_raw(signal: np.ndarray, window: int = WINDOW,
                        step: int = STEP,
                        timestamps: np.ndarray | None = None,
                        ) -> tuple[np.ndarray, np.ndarray | None]:
    """Extract raw (non-featurized) windows for SSL.

    Returns (windows [N, window], win_times [N, 2] or None).
    Windows that are all-NaN are skipped; partial NaNs are linearly
    interpolated so the transformer sees continuous inputs.
    """
    wins, times = [], []
    for i in range(0, len(signal) - window, step):
        w = signal[i:i + window].astype(np.float64)
        if np.isnan(w).all():
            continue
        if np.isnan(w).any():
            # Linear interpolate small gaps; if >50% missing, skip
            nan_frac = np.isnan(w).mean()
            if nan_frac > 0.5:
                continue
            idx = np.arange(len(w))
            good = ~np.isnan(w)
            w = np.interp(idx, idx[good], w[good])
        wins.append(w.astype(np.float32))
        if timestamps is not None:
            try:
                times.append((float(timestamps[i]),
                              float(timestamps[i + window - 1])))
            except Exception:
                times.append((float(i), float(i + window - 1)))
    if not wins:
        return np.zeros((0, window), np.float32), None
    W = np.stack(wins)
    T = np.array(times) if times else None
    return W, T


def load_labels(path: str) -> dict:
    """Same format as ml_supervised_train: (proj, patient, signal) -> segments."""
    import csv, re
    labels = {}
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            proj = row['project'].strip()
            pat = row['patient'].strip().lower()
            m = re.search(r'patient\s*(\d+)', pat)
            pat_key = f"patient {m.group(1)}" if m else pat
            sig = row['signal'].strip()
            key = (proj.upper(), pat_key, sig)
            seg = (float(row['start_s']), float(row['end_s']),
                   1 if row['label'].strip().lower() == 'artifact' else 0)
            labels.setdefault(key, []).append(seg)
    for k in labels:
        labels[k].sort()
    n_seg = sum(len(v) for v in labels.values())
    print(f"[ssl] loaded {n_seg} label segments for {len(labels)} keys",
          flush=True)
    return labels


def label_for_window(segments: list, ws: float, we: float) -> int | None:
    votes = []
    for s, e, lab in segments:
        if e >= ws and s <= we:
            overlap = min(e, we) - max(s, ws)
            if overlap > 0:
                votes.append((overlap, lab))
    if not votes:
        return None
    art_w = sum(w for w, l in votes if l == 1)
    clean_w = sum(w for w, l in votes if l == 0)
    return 1 if art_w >= clean_w else 0


# ----------------------------------------------------------------------------
# Augmentations (two views for contrastive learning)
# ----------------------------------------------------------------------------

def augment_time_mask(w: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    out = w.copy()
    n = len(out)
    mlen = rng.integers(max(1, n // 10), max(2, n // 5))
    start = rng.integers(0, max(1, n - mlen))
    out[start:start + mlen] = 0.0
    return out


def augment_amplitude_scale(w: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    return (w * rng.uniform(0.8, 1.2)).astype(np.float32)


def augment_noise(w: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    std = float(np.std(w)) + 1e-6
    return (w + rng.normal(0, 0.02 * std, size=w.shape)).astype(np.float32)


def augment_time_warp(w: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    n = len(w)
    warp = rng.uniform(0.9, 1.1)
    x_old = np.linspace(0, 1, n)
    # warp the time axis slightly, then resample back to n points
    x_new = np.linspace(0, 1, int(n * warp))
    y_new = np.interp(x_new, x_old, w)
    return np.interp(x_old, x_new, y_new).astype(np.float32)


AUGMENT_FNS = [augment_time_mask, augment_amplitude_scale,
               augment_noise, augment_time_warp]


def two_views(w: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Two independently augmented views of one window."""
    f1, f2 = rng.choice(len(AUGMENT_FNS), size=2, replace=False)
    return AUGMENT_FNS[f1](w, rng), AUGMENT_FNS[f2](w, rng)


# ----------------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------------

class SinusoidalPositionalEncoding(nn.Module):
    def __init__(self, d_model: int, max_len: int = 512):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        pos = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div = torch.exp(torch.arange(0, d_model, 2).float()
                        * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        self.register_buffer('pe', pe.unsqueeze(0))  # [1, max_len, d_model]

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.pe[:, :x.size(1), :]


class SignalTransformer(nn.Module):
    """Transformer encoder for 1-channel physiological windows."""

    def __init__(self, d_model: int = D_MODEL, n_layers: int = N_LAYERS,
                 n_heads: int = N_HEADS, d_ff: int = D_FF,
                 dropout: float = DROPOUT, proj_dim: int = PROJ_DIM):
        super().__init__()
        self.d_model = d_model
        self.input_proj = nn.Linear(1, d_model)
        self.pos_enc = SinusoidalPositionalEncoding(d_model)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=n_heads, dim_feedforward=d_ff,
            dropout=dropout, batch_first=True)
        self.encoder = nn.TransformerEncoder(layer, num_layers=n_layers)
        self.dropout = nn.Dropout(dropout)
        # Contrastive projection head (pretraining)
        self.proj_head = nn.Sequential(
            nn.Linear(d_model, d_model),
            nn.ReLU(),
            nn.Linear(d_model, proj_dim),
        )
        # Classification head (fine-tuning)
        self.cls_head = nn.Linear(d_model, 2)

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, T] -> pooled representation [B, d_model]."""
        h = self.input_proj(x.unsqueeze(-1))      # [B, T, d_model]
        h = self.pos_enc(h)
        h = self.dropout(h)
        h = self.encoder(h)                        # [B, T, d_model]
        return h.mean(dim=1)                       # mean pooling [B, d_model]

    def forward_pretrain(self, x: torch.Tensor) -> torch.Tensor:
        z = self.encode(x)
        return F.normalize(self.proj_head(z), dim=1)  # [B, proj_dim]

    def forward_classify(self, x: torch.Tensor) -> torch.Tensor:
        return self.cls_head(self.encode(x))       # [B, 2] logits


def smooth_infonce(z1: torch.Tensor, z2: torch.Tensor,
                   temperature: float = TEMPERATURE,
                   label_smooth: float = LABEL_SMOOTH) -> torch.Tensor:
    """NT-Xent with label smoothing (Smooth InfoNCE, Le et al. 2025).

    z1, z2: [B, D] L2-normalized projections of two augmented views.
    Positive pairs are (z1[i], z2[i]); all others are negatives.
    """
    B = z1.size(0)
    z = torch.cat([z1, z2], dim=0)                 # [2B, D]
    sim = torch.mm(z, z.t()) / temperature         # [2B, 2B]
    # Mask self-similarity
    mask = torch.eye(2 * B, device=z.device).bool()
    sim.masked_fill_(mask, -1e9)
    # Positive indices: i <-> i+B
    pos = torch.arange(2 * B, device=z.device)
    pos = (pos + B) % (2 * B)
    # Smoothed target: (1-eps) on positive, eps/(2B-2) on negatives
    n_neg = 2 * B - 2
    target = torch.full_like(sim, label_smooth / max(n_neg, 1))
    target[torch.arange(2 * B), pos] = 1.0 - label_smooth
    target[mask] = 0.0
    log_prob = F.log_softmax(sim, dim=1)
    loss = -(target * log_prob).sum(dim=1).mean()
    return loss


# ----------------------------------------------------------------------------
# Datasets
# ----------------------------------------------------------------------------

class ContrastiveDataset(Dataset):
    """Unlabeled windows; each item returns two augmented views."""

    def __init__(self, windows: np.ndarray, seed: int = 0):
        self.windows = windows
        # Per-window z-score normalization (robust to amplitude shifts)
        mu = windows.mean(axis=1, keepdims=True)
        sd = windows.std(axis=1, keepdims=True) + 1e-6
        self.normed = ((windows - mu) / sd).astype(np.float32)
        self.rng = np.random.default_rng(seed)

    def __len__(self):
        return len(self.normed)

    def __getitem__(self, i):
        w = self.normed[i]
        v1, v2 = two_views(w, self.rng)
        return torch.from_numpy(v1), torch.from_numpy(v2)


class LabeledDataset(Dataset):
    def __init__(self, windows: np.ndarray, labels: np.ndarray):
        mu = windows.mean(axis=1, keepdims=True)
        sd = windows.std(axis=1, keepdims=True) + 1e-6
        self.X = ((windows - mu) / sd).astype(np.float32)
        self.y = labels.astype(np.int64)

    def __len__(self):
        return len(self.X)

    def __getitem__(self, i):
        return torch.from_numpy(self.X[i]), torch.tensor(self.y[i])


# ----------------------------------------------------------------------------
# Data loading from Azure-downloaded parquets
# ----------------------------------------------------------------------------

def collect_windows(parquet_dir: Path, signal: str | None,
                    max_files: int = 0, max_windows: int = 0,
                    labels_csv: str | None = None,
                    need_labels: bool = False,
                    ) -> tuple[np.ndarray, np.ndarray | None]:
    """Walk parquet dir, extract raw windows for `signal` (or all signals).

    need_labels=True: also align PROMISES labels; windows without a real
    label fall back to pseudo-label (>20% NaN -> artifact).
    Returns (windows, labels or None).
    """
    real_labels = load_labels(labels_csv) if labels_csv else {}
    files = sorted(Path(parquet_dir).rglob("*filtered.parquet*"))
    if max_files:
        files = files[:max_files]
    print(f"[ssl] {len(files)} parquet files", flush=True)

    all_w, all_y = [], []
    rng = np.random.default_rng(0)
    for f in files:
        try:
            df = read_parquet_any(f)
        except Exception as e:
            print(f"[ssl] skip {f.name}: {e}", flush=True)
            continue
        cols = signal_columns(df)
        if signal and signal.lower() != "all":
            cols = [c for c in cols if c == signal or
                    c.lower() == signal.lower()]
            if not cols:
                continue
        pat = patient_from_path(f)
        proj = project_from_path(f)
        ts = None
        for tc in df.columns:
            if tc.lower() in ("timestamp", "time", "t"):
                try:
                    ts = pd.to_numeric(df[tc], errors="coerce").values
                except Exception:
                    pass
                break
        for col in cols:
            try:
                sig = pd.to_numeric(df[col], errors="coerce").values
            except Exception:
                continue
            if sig is None or len(sig) < WINDOW or np.isnan(sig).all():
                continue
            W, T = extract_windows_raw(sig, timestamps=ts)
            if len(W) == 0:
                continue
            y = None
            if need_labels:
                segs = real_labels.get((proj, pat, col)) if (proj and pat) else None
                y = np.zeros(len(W), dtype=np.int64)
                n_real = 0
                for i in range(len(W)):
                    lab = None
                    if segs is not None and T is not None:
                        lab = label_for_window(segs, float(T[i][0]),
                                               float(T[i][1]))
                        if lab is not None:
                            n_real += 1
                    if lab is None:
                        # pseudo-label fallback
                        lab = 1 if float(np.isnan(sig[i*STEP:i*STEP+WINDOW]).mean()) > 0.2 else 0
                    y[i] = lab
                # skip files with no usable label diversity handled later
            # cap per-file windows
            if len(W) > 2000:
                idx = rng.choice(len(W), 2000, replace=False)
                W = W[idx]
                if y is not None:
                    y = y[idx]
            all_w.append(W)
            if y is not None:
                all_y.append(y)
            if max_windows and sum(len(w) for w in all_w) >= max_windows:
                break
        if max_windows and sum(len(w) for w in all_w) >= max_windows:
            break
    if not all_w:
        return np.zeros((0, WINDOW), np.float32), None
    W = np.concatenate(all_w, axis=0)
    Y = np.concatenate(all_y, axis=0) if all_y else None
    if max_windows and len(W) > max_windows:
        idx = rng.choice(len(W), max_windows, replace=False)
        W = W[idx]
        if Y is not None:
            Y = Y[idx]
    print(f"[ssl] collected {len(W)} windows"
          + (f", {Y.mean():.1%} artifact" if Y is not None else ""),
          flush=True)
    return W, Y


# ----------------------------------------------------------------------------
# Pretraining
# ----------------------------------------------------------------------------

def pretrain(parquet_dir: Path, out: Path, signal: str | None,
             max_files: int = 100, max_windows: int = 200000,
             epochs: int = 10, batch_size: int = 256, lr: float = 3e-4,
             device: str = "cpu", seed: int = 0) -> Path:
    torch.manual_seed(seed)
    np.random.seed(seed)
    W, _ = collect_windows(parquet_dir, signal, max_files=max_files,
                           max_windows=max_windows)
    if len(W) < 1000:
        raise RuntimeError(f"not enough windows for pretraining: {len(W)}")
    ds = ContrastiveDataset(W, seed=seed)
    dl = DataLoader(ds, batch_size=batch_size, shuffle=True,
                    num_workers=0, drop_last=True)
    model = SignalTransformer().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    model.train()
    history = []
    for ep in range(epochs):
        tot, n = 0.0, 0
        for v1, v2 in dl:
            v1, v2 = v1.to(device), v2.to(device)
            z1 = model.forward_pretrain(v1)
            z2 = model.forward_pretrain(v2)
            loss = smooth_infonce(z1, z2)
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            tot += loss.item()
            n += 1
        avg = tot / max(n, 1)
        history.append(avg)
        print(f"[ssl-pretrain] epoch {ep+1}/{epochs} loss {avg:.4f} lr {sched.get_last_lr()[0]:.2e}",
              flush=True)
        sched.step()
    out.mkdir(parents=True, exist_ok=True)
    safe = sanitize(signal or "all")
    enc_path = out / f"{safe}_encoder.pt"
    torch.save({"state_dict": model.state_dict(),
                "config": {"d_model": D_MODEL, "n_layers": N_LAYERS,
                           "n_heads": N_HEADS, "d_ff": D_FF,
                           "dropout": DROPOUT, "proj_dim": PROJ_DIM,
                           "window": WINDOW},
                "signal": signal, "history": history}, enc_path)
    (out / f"{safe}_pretrain_log.json").write_text(json.dumps({
        "signal": signal, "n_windows": len(W), "epochs": epochs,
        "batch_size": batch_size, "loss_history": history,
    }, indent=1))
    print(f"[ssl-pretrain] saved encoder -> {enc_path}", flush=True)
    return enc_path


# ----------------------------------------------------------------------------
# Fine-tuning
# ----------------------------------------------------------------------------

def finetune(parquet_dir: Path, out: Path, signal: str | None,
             pretrained: Path | None, labels_csv: str | None,
             max_files: int = 200, max_windows: int = 50000,
             epochs: int = 15, batch_size: int = 256, lr: float = 1e-4,
             freeze_epochs: int = 3, device: str = "cpu",
             seed: int = 42) -> Path:
    torch.manual_seed(seed)
    np.random.seed(seed)
    W, Y = collect_windows(parquet_dir, signal, max_files=max_files,
                           max_windows=max_windows, labels_csv=labels_csv,
                           need_labels=True)
    if Y is None or len(np.unique(Y)) < 2:
        raise RuntimeError("need both classes for fine-tuning")
    # Stratified 70/15/15
    from sklearn.model_selection import train_test_split
    idx = np.arange(len(W))
    i_tr, i_te = train_test_split(idx, test_size=0.15, random_state=seed,
                                  stratify=Y)
    i_tr2, i_va = train_test_split(i_tr, test_size=0.176,
                                   random_state=seed, stratify=Y[i_tr])
    tr = DataLoader(LabeledDataset(W[i_tr2], Y[i_tr2]), batch_size=batch_size,
                    shuffle=True)
    va = DataLoader(LabeledDataset(W[i_va], Y[i_va]), batch_size=batch_size)
    te = DataLoader(LabeledDataset(W[i_te], Y[i_te]), batch_size=batch_size)

    model = SignalTransformer().to(device)
    if pretrained and Path(pretrained).exists():
        ckpt = torch.load(pretrained, map_location=device, weights_only=False)
        # Load encoder + proj weights; cls head stays random
        sd = {k: v for k, v in ckpt["state_dict"].items()
              if not k.startswith("cls_head")}
        model.load_state_dict(sd, strict=False)
        print(f"[ssl-finetune] loaded pretrained encoder from {pretrained}",
              flush=True)
    else:
        print("[ssl-finetune] no pretrained encoder - training from scratch",
              flush=True)

    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    ce = nn.CrossEntropyLoss()
    best_va, best_sd, history = -1.0, None, []

    def run_epoch(loader, train: bool):
        model.train(train)
        tot, n, correct, total = 0.0, 0, 0, 0
        for xb, yb in loader:
            xb, yb = xb.to(device), yb.to(device)
            logits = model.forward_classify(xb)
            loss = ce(logits, yb)
            if train:
                opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
            tot += loss.item()
            n += 1
            correct += (logits.argmax(1) == yb).sum().item()
            total += len(yb)
        return tot / max(n, 1), correct / max(total, 1)

    for ep in range(epochs):
        # Linear probe phase: freeze encoder
        for p in model.encoder.parameters():
            p.requires_grad = ep >= freeze_epochs
        for p in model.input_proj.parameters():
            p.requires_grad = ep >= freeze_epochs
        tr_loss, tr_acc = run_epoch(tr, True)
        va_loss, va_acc = run_epoch(va, False)
        history.append({"epoch": ep + 1, "train_loss": tr_loss,
                        "train_acc": tr_acc, "val_loss": va_loss,
                        "val_acc": va_acc})
        print(f"[ssl-finetune] epoch {ep+1}/{epochs} "
              f"tr_loss {tr_loss:.4f} tr_acc {tr_acc:.3f} "
              f"va_loss {va_loss:.4f} va_acc {va_acc:.3f}", flush=True)
        if va_acc > best_va:
            best_va = va_acc
            best_sd = {k: v.cpu() for k, v in model.state_dict().items()}

    # Final test evaluation
    model.load_state_dict(best_sd)
    model.eval()
    from sklearn.metrics import accuracy_score, f1_score, roc_auc_score, \
        matthews_corrcoef
    all_p, all_l, all_s = [], [], []
    with torch.no_grad():
        for xb, yb in te:
            logits = model.forward_classify(xb.to(device))
            prob = torch.softmax(logits, dim=1)[:, 1]
            all_p.extend(prob.cpu().numpy())
            all_l.extend(yb.numpy())
            all_s.extend((prob > 0.5).long().cpu().numpy())
    all_p, all_l, all_s = map(np.array, (all_p, all_l, all_s))
    metrics = {
        "test_accuracy": float(accuracy_score(all_l, all_s)),
        "test_f1": float(f1_score(all_l, all_s, zero_division=0)),
        "test_auroc": float(roc_auc_score(all_l, all_p)) if len(np.unique(all_l)) > 1 else 0.0,
        "test_mcc": float(matthews_corrcoef(all_l, all_s)),
        "test_sensitivity": float(all_s[all_l == 1].mean()) if (all_l == 1).any() else 0.0,
        "test_specificity": float(1 - all_s[all_l == 0].mean()) if (all_l == 0).any() else 0.0,
    }
    print(f"[ssl-finetune] TEST " + " ".join(f"{k}={v:.3f}" for k, v in metrics.items()),
          flush=True)

    out.mkdir(parents=True, exist_ok=True)
    safe = sanitize(signal or "all")
    model_path = out / f"{safe}_ssl.pt"
    torch.save({"state_dict": best_sd,
                "config": {"d_model": D_MODEL, "n_layers": N_LAYERS,
                           "n_heads": N_HEADS, "d_ff": D_FF,
                           "dropout": DROPOUT, "proj_dim": PROJ_DIM,
                           "window": WINDOW, "step": STEP},
                "signal": signal, "metrics": metrics,
                "history": history,
                "reliability": "RELIABLE" if metrics["test_f1"] >= 0.8 else "UNRELIABLE",
                "pretrained_from": str(pretrained) if pretrained else None,
                }, model_path)
    (out / f"{safe}_finetune_report.json").write_text(json.dumps({
        "signal": signal, "n_windows": len(W),
        "artifact_frac": float(Y.mean()), **metrics,
        "reliability": "RELIABLE" if metrics["test_f1"] >= 0.8 else "UNRELIABLE",
        "label_source": "labels_csv" if labels_csv else "pseudo",
    }, indent=1))
    print(f"[ssl-finetune] saved -> {model_path}", flush=True)
    return model_path


# ----------------------------------------------------------------------------
# Inference (for smart_filter integration)
# ----------------------------------------------------------------------------

def load_ssl_model(path: str | Path, device: str = "cpu") -> SignalTransformer:
    ckpt = torch.load(path, map_location=device, weights_only=False)
    cfg = ckpt.get("config", {})
    model = SignalTransformer(
        d_model=cfg.get("d_model", D_MODEL),
        n_layers=cfg.get("n_layers", N_LAYERS),
        n_heads=cfg.get("n_heads", N_HEADS),
        d_ff=cfg.get("d_ff", D_FF),
        dropout=0.0, proj_dim=cfg.get("proj_dim", PROJ_DIM))
    model.load_state_dict(ckpt["state_dict"], strict=False)
    model.eval()
    return model.to(device)


def predict_windows(model: SignalTransformer, windows: np.ndarray,
                    device: str = "cpu", batch_size: int = 512) -> np.ndarray:
    """Artifact probabilities for raw [N, WINDOW] windows."""
    mu = windows.mean(axis=1, keepdims=True)
    sd = windows.std(axis=1, keepdims=True) + 1e-6
    Xn = ((windows - mu) / sd).astype(np.float32)
    probs = []
    model.eval()
    with torch.no_grad():
        for i in range(0, len(Xn), batch_size):
            xb = torch.from_numpy(Xn[i:i + batch_size]).to(device)
            logits = model.forward_classify(xb)
            probs.append(torch.softmax(logits, dim=1)[:, 1].cpu().numpy())
    return np.concatenate(probs) if probs else np.zeros(0)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True,
                    choices=["pretrain", "finetune", "predict"])
    ap.add_argument("--parquet-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--signal", default="all")
    ap.add_argument("--pretrained", default=None)
    ap.add_argument("--model", default=None, help="predict: path to _ssl.pt")
    ap.add_argument("--labels-csv", default=None)
    ap.add_argument("--max-files", type=int, default=100)
    ap.add_argument("--max-windows", type=int, default=200000)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args(argv)

    if not HAS_TORCH:
        print("[ssl] PyTorch not installed. pip install torch", flush=True)
        return 1

    pdir = Path(args.parquet_dir)
    out = Path(args.out)
    sig = None if args.signal.lower() == "all" else args.signal

    if args.mode == "pretrain":
        pretrain(pdir, out, sig, max_files=args.max_files,
                 max_windows=args.max_windows, epochs=args.epochs,
                 batch_size=args.batch_size, lr=args.lr, device=args.device)
    elif args.mode == "finetune":
        finetune(pdir, out, sig,
                 pretrained=Path(args.pretrained) if args.pretrained else None,
                 labels_csv=args.labels_csv, max_files=args.max_files,
                 max_windows=args.max_windows, epochs=args.epochs,
                 batch_size=args.batch_size, lr=args.lr, device=args.device)
    elif args.mode == "predict":
        if not args.model:
            print("[ssl] --model required for predict", flush=True)
            return 1
        model = load_ssl_model(args.model, args.device)
        W, _ = collect_windows(pdir, sig, max_files=args.max_files,
                               max_windows=args.max_windows)
        probs = predict_windows(model, W, args.device)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ssl_predictions.json").write_text(json.dumps({
            "signal": args.signal, "n_windows": len(W),
            "mean_prob": float(probs.mean()) if len(probs) else 0.0,
            "frac_flagged": float((probs > 0.5).mean()) if len(probs) else 0.0,
        }, indent=1))
        print(f"[ssl] predicted {len(probs)} windows", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
