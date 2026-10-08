#!/usr/bin/env python3
"""
multimodal.py — Phase D: Multimodal physiological representation model.

Architecture (per K's spec):
  Waveforms (ECG/PLETH) → CNN encoder → 32D
  Scalars (12 vars)     → MLP+TCN encoder → 64D
  Settings              → Setting encoder → 16D
  Events                → Event encoder → 16D
                                      ↓
                              Fusion → 128D latent Z(t)
                                      ↓
                    ┌─────────────────┼──────────────┐
                    ▼                 ▼              ▼
              QC head (artifact)  Recon head    (future: prediction)

Training: multi-task loss (QC + reconstruction + masked prediction).
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


class WaveformEncoder(nn.Module):
    """1D CNN for ECG/PLETH waveforms. Input: (B, 1, T). Output: (B, 32)."""
    def __init__(self, in_channels: int = 1, out_dim: int = 32):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(in_channels, 32, kernel_size=15, stride=2, padding=7),
            nn.BatchNorm1d(32), nn.GELU(),
            nn.Conv1d(32, 64, kernel_size=11, stride=2, padding=5),
            nn.BatchNorm1d(64), nn.GELU(),
            nn.Conv1d(64, 128, kernel_size=7, stride=2, padding=3),
            nn.BatchNorm1d(128), nn.GELU(),
            nn.AdaptiveAvgPool1d(1),
        )
        self.proj = nn.Linear(128, out_dim)

    def forward(self, x):
        # x: (B, 1, T)
        h = self.net(x).squeeze(-1)  # (B, 128)
        return self.proj(h)  # (B, 32)


class ScalarEncoder(nn.Module):
    """
    Encodes multivariate scalar time series.
    Input: values (B, T, N), mask (B, T, N). Output: (B, 64).
    """
    def __init__(self, n_vars: int, out_dim: int = 64, hidden: int = 128):
        super().__init__()
        # Per-timestep: value + mask concatenated
        self.input_proj = nn.Linear(n_vars * 2, hidden)
        self.tcn = nn.Sequential(
            nn.Conv1d(hidden, hidden, kernel_size=5, padding=2),
            nn.GELU(),
            nn.Conv1d(hidden, hidden, kernel_size=5, padding=2),
            nn.GELU(),
        )
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.proj = nn.Linear(hidden, out_dim)

    def forward(self, values, mask):
        # values, mask: (B, T, N)
        x = torch.cat([values, mask], dim=-1)  # (B, T, 2N)
        x = self.input_proj(x)  # (B, T, H)
        x = x.transpose(1, 2)  # (B, H, T) for Conv1d
        x = self.tcn(x)  # (B, H, T)
        x = self.pool(x).squeeze(-1)  # (B, H)
        return self.proj(x)  # (B, 64)


class SettingEncoder(nn.Module):
    """
    Encodes device settings as state variables.
    Input: settings (B, S), time_since_change (B, S). Output: (B, 16).
    """
    def __init__(self, n_settings: int, out_dim: int = 16):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_settings * 2, 32),
            nn.GELU(),
            nn.Linear(32, out_dim),
        )

    def forward(self, values, time_since_change):
        x = torch.cat([values, time_since_change], dim=-1)
        return self.net(x)


class EventEncoder(nn.Module):
    """
    Encodes discrete events. Input: event counts (B, E). Output: (B, 16).
    """
    def __init__(self, n_events: int, out_dim: int = 16):
        super().__init__()
        self.embedding = nn.Embedding(n_events + 1, 16, padding_idx=0)
        self.proj = nn.Linear(16, out_dim)

    def forward(self, event_ids):
        # event_ids: (B, max_events) — 0 = padding
        emb = self.embedding(event_ids)  # (B, max_events, 16)
        # Mean pool over events (ignore padding)
        mask = (event_ids != 0).float().unsqueeze(-1)
        pooled = (emb * mask).sum(1) / mask.sum(1).clamp(min=1)
        return self.proj(pooled)


class MultimodalPhysioModel(nn.Module):
    """
    Full multimodal model. Fuses waveform + scalar + setting + event
    encoders into a 128-dim latent physiological state.
    """
    def __init__(self, n_scalars: int = 12, n_settings: int = 6,
                 n_events: int = 10):
        super().__init__()
        self.ecg_encoder = WaveformEncoder(out_dim=32)
        self.pleth_encoder = WaveformEncoder(out_dim=32)
        self.scalar_encoder = ScalarEncoder(n_scalars, out_dim=64)
        self.setting_encoder = SettingEncoder(n_settings, out_dim=16)
        self.event_encoder = EventEncoder(n_events, out_dim=16)

        # Fusion: 32+32+64+16+16 = 160 → 128
        self.fusion = nn.Sequential(
            nn.Linear(160, 256),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(256, 128),
        )

        # Heads
        self.qc_head = nn.Linear(128, 1)  # artifact probability
        self.recon_head = nn.Linear(128, n_scalars)  # reconstruct scalars

    def forward(self, batch: dict):
        ecg_z = self.ecg_encoder(batch["ecg"])           # (B, 32)
        pleth_z = self.pleth_encoder(batch["pleth"])     # (B, 32)
        scalar_z = self.scalar_encoder(
            batch["scalars"], batch["scalar_mask"])      # (B, 64)
        setting_z = self.setting_encoder(
            batch["settings"], batch["settings_dt"])     # (B, 16)
        event_z = self.event_encoder(batch["events"])    # (B, 16)

        z = torch.cat([ecg_z, pleth_z, scalar_z, setting_z, event_z], dim=-1)
        z = self.fusion(z)  # (B, 128)

        return {
            "latent": z,
            "qc_logit": self.qc_head(z).squeeze(-1),      # (B,)
            "reconstruction": self.recon_head(z),          # (B, n_scalars)
        }


def multimodal_loss(outputs: dict, targets: dict,
                    lambda_recon: float = 1.0,
                    lambda_qc: float = 1.0) -> dict:
    """Multi-task loss: BCE for QC + MSE for reconstruction."""
    qc_loss = F.binary_cross_entropy_with_logits(
        outputs["qc_logit"], targets["is_artifact"].float()
    )
    # Reconstruction only on valid (non-artifact) samples
    valid = (targets["is_artifact"] == 0)
    if valid.sum() > 0:
        recon_loss = F.mse_loss(
            outputs["reconstruction"][valid],
            targets["scalars_mean"][valid],
        )
    else:
        recon_loss = torch.tensor(0.0)

    total = lambda_qc * qc_loss + lambda_recon * recon_loss
    return {"total": total, "qc": qc_loss, "recon": recon_loss}


if __name__ == "__main__":
    # Architecture self-test with synthetic batch
    torch.manual_seed(0)
    B, T_wave, T_scalar = 4, 7500, 30  # 30s at 250Hz / 1Hz
    N_scalar, N_set, N_evt = 12, 6, 10

    batch = {
        "ecg": torch.randn(B, 1, T_wave),
        "pleth": torch.randn(B, 1, T_wave // 2),  # 125 Hz
        "scalars": torch.randn(B, T_scalar, N_scalar),
        "scalar_mask": torch.ones(B, T_scalar, N_scalar),
        "settings": torch.randn(B, N_set),
        "settings_dt": torch.rand(B, N_set) * 600,
        "events": torch.randint(0, N_evt + 1, (B, 5)),
    }
    # Fix pleth length mismatch for test (pad to match ecg encoder input)
    batch["pleth"] = torch.randn(B, 1, T_wave)

    model = MultimodalPhysioModel(n_scalars=N_scalar, n_settings=N_set,
                                   n_events=N_evt)
    out = model(batch)
    print(f"Latent shape: {out['latent'].shape} (expected: [{B}, 128])")
    print(f"QC logit shape: {out['qc_logit'].shape} (expected: [{B}])")
    print(f"Recon shape: {out['reconstruction'].shape} (expected: [{B}, {N_scalar}])")

    # Test loss
    targets = {
        "is_artifact": torch.randint(0, 2, (B,)),
        "scalars_mean": torch.randn(B, N_scalar),
    }
    losses = multimodal_loss(out, targets)
    print(f"\nLosses: total={losses['total'].item():.4f}, "
          f"qc={losses['qc'].item():.4f}, recon={losses['recon'].item():.4f}")

    # Test backward pass
    losses["total"].backward()
    print("\n✓ Backward pass successful")
    print("✓ Multimodal architecture functional")
