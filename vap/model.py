import torch
import torch.nn as nn
import torch.nn.functional as F
from torchmetrics.classification.f_beta import F1Score
from torchmetrics.classification.precision_recall_curve import PrecisionRecallCurve
import pytorch_lightning as pl
from einops.layers.torch import Rearrange
from typing import Optional, Dict

from vap.encoder import Encoder
from vap.transformer import GPT, GPTStereo
from vap.utils import everything_deterministic

from vap_turn_taking.vap_new import VAP
from vap_turn_taking.events import TurnTakingEventsNew

everything_deterministic()


def loss_fn_va(v1: torch.Tensor, v2: torch.Tensor, va: torch.Tensor) -> torch.Tensor:
    """Loss for Voice Activity classification (BCE)"""
    n_batch, n_frames, _ = v1.shape  # (B, N_FRAMES, 1)
    v1_label = va[:, :n_frames, :1]  # -> (B, N_FRAMES, 1)
    v2_label = va[:, :n_frames, 1:]  # -> (B, N_FRAMES, 1)
    l1 = F.binary_cross_entropy_with_logits(v1, v1_label)
    l2 = F.binary_cross_entropy_with_logits(v2, v2_label)
    return (l1 + l2) / 2


class VAPHead(nn.Module):
    def __init__(
        self,
        input_dim: int,
        n_bins: int = 4,
        representation: str = "discrete",
        bias_w_distribution: bool = True,
    ):
        super().__init__()
        self.representation = representation
        self.output_dim = 1
        if self.representation == "comparative":
            self.projection_head = nn.Linear(input_dim, 1)
        else:
            self.total_bins = 2 * n_bins
            if self.representation == "independent":
                self.projection_head = nn.Sequential(
                    nn.Linear(input_dim, self.total_bins),
                    Rearrange("... (c f) -> ... c f", c=2, f=self.total_bins // 2),
                )
                self.output_dim = (2, n_bins)
            else:
                self.n_classes = 2 ** self.total_bins
                self.projection_head = nn.Linear(input_dim, self.n_classes)
                self.output_dim = self.n_classes
                if bias_w_distribution:
                    self.projection_head.bias.data = torch.load(
                        "example/label_probs.pt"
                    ).log()

    def __repr__(self):
        s = "VAPHead\n"
        s += f"  representation: {self.representation}"
        s += f"  output: {self.output_dim}"
        return super().__repr__()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.projection_head(x)


class VACondition(nn.Module):
    def __init__(
        self, dim: int, va_history: bool = False, va_history_bins: int = 5
    ) -> None:
        super().__init__()
        self.dim = dim
        self.va_history = va_history
        self.va_history_bins = va_history_bins
        self.va_cond = nn.Linear(2, dim)  # va: 2 one-hot encodings -> dim
        self.ln = nn.LayerNorm(dim)
        if va_history:
            # vah: (N, vah_bins) -> dim
            self.va_hist_cond = nn.Linear(va_history_bins, dim)

    def forward(
        self, vad: torch.Tensor, va_history: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        v_cond = self.va_cond(vad)

        # Add vad-history information
        if self.va_history and va_history is not None:
            v_cond += self.va_hist_cond(va_history)

        return self.ln(v_cond)


class ProjectionModel(nn.Module):
    def __init__(self, conf) -> None:
        super().__init__()
        self.conf = conf
        self.stereo = conf.get("stereo", False)
        self.frame_hz = conf["frame_hz"]
        self.sample_rate = conf["sample_rate"]

        # Audio Encoder
        self.encoder = Encoder(
            freeze=conf["encoder"].get("freeze", True),
            downsample=conf["encoder"].get("downsample", None),
        )

        if self.encoder.output_dim != conf["ar"]["dim"]:
            self.projection = nn.Linear(self.encoder.output_dim, conf["ar"]["dim"])
        else:
            self.projection = nn.Identity()

        # VAD Conditioning
        if self.stereo:
            self.ar_channel = GPT(
                dim=conf["ar"]["dim"],
                dff_k=conf["ar"]["dff_k"],
                num_layers=conf["ar"]["channel_layers"],
                num_heads=conf["ar"]["num_heads"],
                dropout=conf["ar"]["dropout"],
            )
        else:
            self.vad_condition = VACondition(
                dim=conf["ar"]["dim"],
                va_history=conf["va_cond"]["history"],
                va_history_bins=conf["va_cond"]["history_bins"],
            )

        # Autoregressive
        AR = GPTStereo if self.stereo else GPT
        self.ar = AR(
            dim=conf["ar"]["dim"],
            dff_k=conf["ar"]["dff_k"],
            num_layers=conf["ar"]["num_layers"],
            num_heads=conf["ar"]["num_heads"],
            dropout=conf["ar"]["dropout"],
        )

        # VAD objective -> x1, x2 -> logits ->  BCE
        if self.stereo:
            self.va_classifier = nn.Linear(conf["ar"]["dim"], 1)

        # Appropriate VAP-head
        self.vap_representation = conf["vap"]["type"]
        self.vap_head = VAPHead(
            input_dim=conf["ar"]["dim"],
            n_bins=len(conf["vap"]["bin_times"]),
            representation=self.vap_representation,
        )

    def forward(
        self,
        waveform: torch.Tensor,
        va: Optional[torch.Tensor] = None,
        va_history: Optional[torch.Tensor] = None,
    ) -> Dict[str, torch.Tensor]:

        ret = {}  # return dict
        if self.stereo:
            # Placeholder before defining architecture
            assert (
                waveform.shape[1] == 2
            ), f"Expects 2 channels (B, 2, n_samples) got {waveform.shape}"
            x1 = self.encoder(waveform[:, :1])  # speaker 1
            x2 = self.encoder(waveform[:, 1:])  # speaker 2
            x1 = self.projection(x1)
            x2 = self.projection(x2)
            # Autoregressive
            x1 = self.ar_channel(x1)["x"]
            x2 = self.ar_channel(x2)["x"]
            out = self.ar(x1, x2)

            # Vad Objective
            ret["v1"] = self.va_classifier(out["x1"])
            ret["v2"] = self.va_classifier(out["x2"])

            # projection
            z = out["x"]
        else:
            assert va is not None, "Requires voice-activity input but va=None"
            z = self.encoder(waveform)
            z = self.projection(z)

            # Ugly: sometimes you may get an extra frame from waveform encoding
            z = z[:, : va.shape[1]]

            # Vad conditioning... extra frames... Also Ugly...
            vc = self.vad_condition(va, va_history)[:, : z.shape[1]]

            # Add vad-conditioning to audio features
            z = z + vc
            # Autoregressive
            z = self.ar(z)["x"]
        ret["logits"] = self.vap_head(z)
        return ret


class VAPModel(pl.LightningModule):
    def __init__(self, conf) -> None:
        super().__init__()
        self.conf = conf
        self.stereo = conf["model"].get("stereo", False)
        self.frame_hz = conf["model"]["frame_hz"]
        self.sample_rate = conf["model"]["sample_rate"]
        self.audio_duration_training = conf["model"]["audio_duration"]

        # Model
        self.net: nn.Module = ProjectionModel(conf["model"])

        # Training params
        self.learning_rate = conf["optimizer"]["learning_rate"]

        # VAP: labels, logits -> zero-shot probs
        sh_opts = self.conf["events"]["shift_hold"]
        bc_opts = self.conf["events"]["backchannel"]
        sl_opts = self.conf["events"]["long_short"]
        mt_opts = self.conf["events"]["metric"]
        self.event_extractor = TurnTakingEventsNew(
            sh_pre_cond_time=sh_opts["pre_cond_time"],
            sh_post_cond_time=sh_opts["post_cond_time"],
            sh_prediction_region_on_active=sh_opts["post_cond_time"],
            bc_pre_cond_time=bc_opts["pre_cond_time"],
            bc_post_cond_time=bc_opts["post_cond_time"],
            bc_max_duration=bc_opts["max_duration"],
            bc_negative_pad_left_time=bc_opts["negative_pad_left_time"],
            bc_negative_pad_right_time=bc_opts["negative_pad_right_time"],
            prediction_region_time=mt_opts["prediction_region_time"],
            long_onset_region_time=sl_opts["onset_region_time"],
            long_onset_condition_time=sl_opts["onset_condition_time"],
            min_context_time=mt_opts["min_context"],
            metric_time=mt_opts["pad_time"],
            metric_pad_time=mt_opts["pad_time"],
            max_time=self.audio_duration_training,
            frame_hz=self.frame_hz,
            equal_hold_shift=sh_opts["pre_cond_time"],
        )
        self.VAP: nn.Module = VAP(
            objective=conf["model"]["vap"]["type"],
            bin_times=conf["model"]["vap"]["bin_times"],
            frame_hz=conf["model"]["frame_hz"],
            pre_frames=conf["model"]["vap"]["pre_frames"],
            threshold_ratio=conf["model"]["vap"]["bin_threshold"],
        )
        self.vad_history_times = self.conf["data"]["vad_history_times"]
        self.horizon = self.VAP.horizon
        self.horizon_time = self.VAP.horizon_time
        self.save_hyperparameters()

        # Metrics
        self.val_hs_metric = F1Score(num_classes=2, average="weighted", multiclass=True)
        self.val_ls_metric = F1Score(num_classes=2, average="weighted", multiclass=True)
        self.val_sp_metric = F1Score(num_classes=2, average="weighted", multiclass=True)
        self.val_bp_metric = F1Score(num_classes=2, average="weighted", multiclass=True)

    @property
    def run_name(self):
        s = "VAP"
        s += f"_{self.frame_hz}Hz"
        s += f"_ad{self.audio_duration_training}s"
        s += f"_{self.conf['model']['ar']['channel_layers']}"
        s += str(self.conf["model"]["ar"]["num_layers"])
        s += str(self.conf["model"]["ar"]["num_heads"])
        if not self.stereo:
            s += "_mono"
        return s

    def forward(
        self,
        waveform: torch.Tensor,
        va: Optional[torch.Tensor] = None,
        va_history: Optional[torch.Tensor] = None,
    ) -> Dict[str, torch.Tensor]:
        assert (
            waveform.ndim == 3
        ), f"Expects (B, N_CHANNEL, N_SAMPLES) got {waveform.shape}"

        if va is not None:
            assert va.ndim == 3, f"Expects (B, N_FRAMES, 2) got {va.shape}"

        if va_history is not None:
            assert (
                va_history.ndim == 3
            ), f"Expects (B, N_FRAMES, 5) got {va_history.shape}"

        # net returns: 'logits' and Optionally (if stereo) 'v1' and 'v2' for vad classification
        return self.net(waveform, va=va, va_history=va_history)

    @torch.no_grad()
    def output(
        self,
        waveform: torch.Tensor,
        va: Optional[torch.Tensor] = None,
        va_history: Optional[torch.Tensor] = None,
        max_time: Optional[float] = None,
    ):

        assert (
            waveform.ndim == 3
        ), f"Expects waveform of shape (B, C, n_sample) got {waveform.shape}"

        if va is not None:
            assert (
                va.ndim == 3
            ), f"Expects waveform of shape (B, n_frames, 2) got {va.shape}"

        if va_history is not None:
            assert (
                va_history.ndim == 3
            ), f"Expects waveform of shape (B, n_frames, 5) got {va_history.shape}"

        def pad_va(va):
            b = va.shape[0]
            zero_pad = torch.zeros((b, self.horizon, 2), device=va.device)
            return torch.cat((va, zero_pad), dim=1)

        out = self(waveform, va, va_history)

        if "v1" in out:
            vad_logits = torch.cat((out["v1"], out["v2"]), dim=-1)
            out["vad"] = vad_logits.sigmoid()
            out.pop("v1")
            out.pop("v2")
            if va is None:
                # pad horizon for labels
                va = pad_va(out["vad"])
                vap_out = self.VAP(logits=out["logits"], va=va)
                out.update(vap_out)
            va_oh = (out["vad"] > 0.5) * 1
            events = self.event_extractor(va_oh, max_time=max_time)
            out.update(events)

        else:
            vapad = pad_va(va)
            vap_out = self.VAP(logits=out["logits"], va=vapad)
            out.update(vap_out)
        return out

    def shared_step(
        self, batch: Dict, reduction: str = "mean"
    ) -> Dict[str, torch.Tensor]:
        """
        Arguments:
            batch:      dict, containing 'waveform', va, va_history

        Returns:
            loss:       torch.Tensor
            out:        dict
            batch:      same as input arguments (fixed for differenct encoder Hz)
        """

        n_max_frames = batch["vad"].shape[1] - self.horizon

        ########################################
        # VA-history
        ########################################
        vah_input = None
        if "vad_history" in batch:
            vah_input = batch["vad_history"][:, :n_max_frames]

        ########################################
        # Forward pass -> logits: torch.Tensor
        ########################################
        out = self(
            waveform=batch["waveform"],
            va=batch["vad"][:, :n_max_frames],
            va_history=vah_input,
        )

        ########################################
        # VAP-Head: Extract Probs and Labels
        vap_out = self.VAP(logits=out["logits"], va=batch["vad"])
        out.update(vap_out)
        ########################################

        if "v1" in out:
            loss_va = loss_fn_va(v1=out["v1"], v2=out["v2"], va=batch["vad"])
            out["loss_va"] = loss_va

        loss = self.VAP.loss_fn(out["logits"], out["labels"], reduction=reduction)
        out["loss"] = loss
        return out

    def configure_optimizers(self) -> Dict:
        opt = torch.optim.AdamW(
            self.parameters(),
            lr=self.learning_rate,
            betas=self.conf["optimizer"]["betas"],
            weight_decay=self.conf["optimizer"]["weight_decay"],
        )
        # lr_scheduler = {
        #         "scheduler": torch.optim.lr_scheduler.CosineAnnealingLR(
        #             optimizer=opt,
        #             T_max=self.conf["optimizer"].get("lr_scheduler_tmax", 10),
        #             last_epoch=-1,
        #         ),
        #         "interval": self.conf["optimizer"].get("lr_scheduler_interval", "step"),
        #         "frequency": self.conf["optimizer"].get("lr_scheduler_freq", 1000),
        #     }
        lr_scheduler = {
            "scheduler": torch.optim.lr_scheduler.ReduceLROnPlateau(
                opt,
                mode="min",
                factor=self.conf["optimizer"]["factor"],
                patience=self.conf["optimizer"]["patience"],
            ),
            "monitor": "val_loss",
        }
        return {"optimizer": opt, "lr_scheduler": lr_scheduler}

    def training_step(self, batch, batch_idx, **kwargs):
        out = self.shared_step(batch)
        batch_size = batch["waveform"].shape[0]
        self.log("loss", out["loss"], batch_size=batch_size, sync_dist=True)

        loss = out["loss"]
        if "loss_va" in out:
            self.log("loss_va", out["loss_va"], batch_size=batch_size, sync_dist=True)
            loss += out["loss_va"]
        return {"loss": loss}

    def validation_step(self, batch, batch_idx, **kwargs):
        """validation step"""

        # Regular forward pass
        out = self.shared_step(batch)
        batch_size = batch["vad"].shape[0]

        # log validation loss
        self.log("val_loss", out["loss"], batch_size=batch_size, sync_dist=True)
        if "loss_va" in out:
            self.log(
                "val_loss_va", out["loss_va"], batch_size=batch_size, sync_dist=True
            )

        # Event Metrics
        events = self.event_extractor(batch["vad"])

        preds, targets = self.VAP.extract_prediction_and_targets(
            p=out["p"], p_bc=out["p_bc"], events=events
        )

        if preds["hs"] is not None:
            self.val_hs_metric(preds=preds["hs"], target=targets["hs"])

        if preds["ls"] is not None:
            self.val_ls_metric(preds=preds["ls"], target=targets["ls"])

        if preds["pred_shift"] is not None:
            self.val_sp_metric(preds=preds["pred_shift"], target=targets["pred_shift"])

        if preds["pred_backchannel"] is not None:
            self.val_bp_metric(
                preds=preds["pred_backchannel"], target=targets["pred_backchannel"]
            )

        # Log
        self.log("val_f1_hs", self.val_hs_metric, on_step=False, on_epoch=True)
        self.log("val_f1_ls", self.val_ls_metric, on_step=False, on_epoch=True)
        self.log("val_f1_pred_sh", self.val_sp_metric, on_step=False, on_epoch=True)
        self.log("val_f1_pred_bc", self.val_bp_metric, on_step=False, on_epoch=True)

    def test_step(self, batch, batch_idx, **kwargs):
        """validation step"""

        if not hasattr(self, "test_hs_metric"):
            # Metrics
            self.test_hs_metric = F1Score(
                num_classes=2, average="weighted", multiclass=True
            )
            self.test_ls_metric = F1Score(
                num_classes=2, average="weighted", multiclass=True
            )
            self.test_sp_metric = F1Score(
                num_classes=2, average="weighted", multiclass=True
            )
            self.test_bp_metric = F1Score(
                num_classes=2, average="weighted", multiclass=True
            )

        # Regular forward pass
        out = self.shared_step(batch)
        batch_size = batch["vad"].shape[0]

        # log validation loss
        self.log("val_loss", out["loss"], batch_size=batch_size, sync_dist=True)
        if "loss_va" in out:
            self.log(
                "val_loss_va", out["loss_va"], batch_size=batch_size, sync_dist=True
            )

        # Event Metrics
        events = self.event_extractor(batch["vad"])
        preds, targets = self.VAP.extract_prediction_and_targets(
            p=out["p"], p_bc=out["p_bc"], events=events
        )

        if preds["hs"] is not None:
            self.test_hs_metric(preds=preds["hs"], target=targets["hs"])

        if preds["ls"] is not None:
            self.test_ls_metric(preds=preds["ls"], target=targets["ls"])

        if preds["pred_shift"] is not None:
            self.test_sp_metric(preds=preds["pred_shift"], target=targets["pred_shift"])

        if preds["pred_backchannel"] is not None:
            self.test_bp_metric(
                preds=preds["pred_backchannel"], target=targets["pred_backchannel"]
            )

        # Log
        self.log("test_f1_hs", self.test_hs_metric, on_step=False, on_epoch=True)
        self.log("test_f1_ls", self.test_ls_metric, on_step=False, on_epoch=True)
        self.log("test_f1_pred_sh", self.test_sp_metric, on_step=False, on_epoch=True)
        self.log("test_f1_pred_bc", self.test_bp_metric, on_step=False, on_epoch=True)


if __name__ == "__main__":

    from os import cpu_count
    from datasets_turntaking import DialogAudioDM
    from vap.utils import load_hydra_conf, batch_to_device

    conf = load_hydra_conf()
    config_name = "model/vap_50hz"  # "model/vap_50hz_stereo"
    config_name = "model/vap_50hz_stereo"  # "model/vap_50hz_stereo"
    conf["model"] = load_hydra_conf(config_name=config_name)["model"]
    model = VAPModel(conf)
    if torch.cuda.is_available():
        model = model.to("cuda")
    print(model.run_name)
    dm = DialogAudioDM(
        datasets=["switchboard", "fisher"],
        audio_duration=conf["data"]["audio_duration"],
        vad_history=conf["model"]["va_cond"]["history"],
        audio_mono=not model.stereo,
        batch_size=2,
        num_workers=cpu_count(),
    )
    dm.prepare_data()
    dm.setup()

    batch = next(iter(dm.val_dataloader()))

    batch = batch_to_device(batch, "cuda")

    out = model.forward(waveform=batch["waveform"], va=batch["vad"])
    print("-" * 50)
    print("FORWARD")
    for k, v in out.items():
        if isinstance(v, torch.Tensor):
            print(f"{k}: {tuple(v.shape)}")
        else:
            print(f"{k}: {v}")

    out = model.shared_step(batch)
    print("")
    print("-" * 50)
    print("SHARED STEP")
    for k, v in out.items():
        if "loss" in k:
            print(k, v)
        elif isinstance(v, torch.Tensor):
            print(f"{k}: {tuple(v.shape)}")
        else:
            print(f"{k}: {v}")

    # trainer = pl.Trainer(accelerator="gpu", devices=-1)
    # trainer.test(model, dataloaders=dm.val_dataloader())
