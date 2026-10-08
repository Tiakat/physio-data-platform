#!/usr/bin/env python3
"""
train_multimodal.py — Train the multimodal model on synthetic data.
Verifies the model LEARNS (loss decreases, QC accuracy improves).
"""

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
import numpy as np
import sys
sys.path.insert(0, "/home/hatch/workspace/build")
from multimodal import MultimodalPhysioModel, multimodal_loss
from synthetic_artifacts import corrupt


class SyntheticMultimodalDataset(Dataset):
    """Synthetic 30s windows with waveforms + scalars + settings + events."""

    def __init__(self, n_samples: int = 500, seed: int = 0):
        self.n = n_samples
        rng = np.random.default_rng(seed)
        self.rng = rng

    def __len__(self):
        return self.n

    def _gen_ecg(self, hr: float, T: int = 7500, seed: int = 0) -> np.ndarray:
        """Simple synthetic ECG: QRS complexes at HR rate."""
        rng = np.random.default_rng(seed)
        t = np.arange(T) / 250.0  # 250 Hz
        ecg = np.zeros(T)
        rr_interval = 60.0 / hr
        for beat_start in np.arange(0, t[-1], rr_interval):
            idx = int(beat_start * 250)
            if idx + 50 < T:
                # Simple QRS: small Q, tall R, small S
                ecg[idx+10:idx+15] -= 0.1
                ecg[idx+15:idx+25] += 1.0 * np.hanning(10)
                ecg[idx+25:idx+35] -= 0.2 * np.hanning(10)
        return ecg + rng.normal(0, 0.05, T)

    def __getitem__(self, idx):
        rng = np.random.default_rng(idx * 7919)
        hr = rng.uniform(60, 100)

        # Waveforms
        ecg = self._gen_ecg(hr, seed=idx)
        pleth = np.sin(2 * np.pi * hr / 60 * np.arange(7500) / 250)
        pleth += rng.normal(0, 0.1, 7500)

        # Maybe corrupt waveforms
        is_artifact = 0
        if rng.random() < 0.4:
            is_artifact = 1
            corrupt_type = rng.choice(["flat", "noise", "spike"])
            if corrupt_type == "flat":
                s = rng.integers(0, 6000)
                ecg[s:s+1500] = ecg[s]  # 6s flatline
            elif corrupt_type == "noise":
                ecg += rng.normal(0, 2.0, 7500)
            else:
                s = rng.integers(0, 7500)
                ecg[s] += 10.0

        # Scalars (12 vars, 30 timesteps at 1Hz)
        scalars = rng.normal(0, 1, (30, 12))
        scalars[:, 0] = hr + rng.normal(0, 1, 30)  # HR ~ true HR
        scalar_mask = np.ones((30, 12))

        # Settings
        settings = rng.normal(0, 1, 6)
        settings_dt = rng.uniform(0, 600, 6)

        # Events
        events = rng.integers(0, 11, 5)

        # Target: mean of scalars (for reconstruction)
        scalars_mean = scalars.mean(0)

        return {
            "ecg": torch.from_numpy(ecg).float().unsqueeze(0),
            "pleth": torch.from_numpy(pleth).float().unsqueeze(0),
            "scalars": torch.from_numpy(scalars).float(),
            "scalar_mask": torch.from_numpy(scalar_mask).float(),
            "settings": torch.from_numpy(settings).float(),
            "settings_dt": torch.from_numpy(settings_dt).float(),
            "events": torch.from_numpy(events).long(),
            "is_artifact": torch.tensor(is_artifact).long(),
            "scalars_mean": torch.from_numpy(scalars_mean).float(),
        }


def main():
    device = torch.device("cpu")
    print(f"Device: {device}")

    train_ds = SyntheticMultimodalDataset(n_samples=800, seed=0)
    val_ds = SyntheticMultimodalDataset(n_samples=200, seed=999)
    train_dl = DataLoader(train_ds, batch_size=16, shuffle=True)
    val_dl = DataLoader(val_ds, batch_size=16)

    model = MultimodalPhysioModel().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3)

    print("\nTraining (5 epochs)...")
    for epoch in range(5):
        model.train()
        train_loss = 0
        for b in train_dl:
            b = {k: v.to(device) if isinstance(v, torch.Tensor) else v
                 for k, v in b.items()}
            out = model(b)
            targets = {"is_artifact": b["is_artifact"],
                       "scalars_mean": b["scalars_mean"]}
            losses = multimodal_loss(out, targets)
            opt.zero_grad()
            losses["total"].backward()
            opt.step()
            train_loss += losses["total"].item()

        # Validation
        model.eval()
        val_loss, correct, total = 0, 0, 0
        with torch.no_grad():
            for b in val_dl:
                b = {k: v.to(device) if isinstance(v, torch.Tensor) else v
                     for k, v in b.items()}
                out = model(b)
                targets = {"is_artifact": b["is_artifact"],
                           "scalars_mean": b["scalars_mean"]}
                losses = multimodal_loss(out, targets)
                val_loss += losses["total"].item()
                pred = (out["qc_logit"] > 0).long()
                correct += (pred == b["is_artifact"]).sum().item()
                total += len(b["is_artifact"])

        acc = correct / total
        print(f"  Epoch {epoch+1}: train_loss={train_loss/len(train_dl):.4f}, "
              f"val_loss={val_loss/len(val_dl):.4f}, val_acc={acc:.3f}")

    print(f"\n{'✓ MULTIMODAL LEARNS' if acc > 0.70 else '✗ NOT LEARNING'} "
          f"(val_acc={acc:.3f})")
    torch.save(model.state_dict(), "/home/hatch/workspace/build/multimodal_v1.pt")
    print("Model saved to multimodal_v1.pt")


if __name__ == "__main__":
    main()
