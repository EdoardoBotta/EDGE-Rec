from .evaluate import compute_metrics_from_ratings, compute_metrics_from_interactions, METRIC_NAMES, DEFAULT_TOP_K

from ..datasets import RatingSubgraphData, DataHolder
from ..diffusion import GaussianDiffusion
from ..utils import tqdm, CopyArgTypes, get_kwargs, DataLogger

from collections import deque
from functools import partial
from multiprocessing import cpu_count
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from accelerate import Accelerator, DataLoaderConfiguration
from ema_pytorch import EMA
import numpy as np
import torch
from torch.optim import Adam
from torch.utils.data import Dataset, DataLoader


def _prepare(accelerator: Accelerator, args: CopyArgTypes) -> CopyArgTypes:
    if isinstance(args, (tuple, list)):
        return accelerator.prepare(*args)
    else:
        return accelerator.prepare(args)


def _get_dataloader(dataset: Optional[Dataset], batch_size: int, accelerator: Optional[Accelerator] = None):
    if dataset is None:
        return None

    loader = DataLoader(
        dataset=dataset,
        batch_size=batch_size,
        shuffle=False,
        pin_memory=True,
        num_workers=cpu_count(),
    )
    if accelerator is not None:
        loader = _prepare(accelerator, loader)
    while True:
        for data in loader:
            yield data


def _get(loader) -> RatingSubgraphData:
    # because the loader strips the wrapping class down to a 'dict'
    return RatingSubgraphData(**next(loader))


class Trainer(object):
    def __init__(
            self,
            # model
            diffusion_model: GaussianDiffusion,
            # dataset
            data_holder: Optional[DataHolder] = None,
            subgraph_size: Optional[int] = None,
            target_density: Optional[float] = None,
            *,
            # training
            batch_size: int = 16,
            gradient_accumulate_every: int = 1,
            force_batch_size: bool = False,
            train_num_steps: int = 100000,
            train_mask_unknown_ratings: bool = True,
            # eval
            eval_batch_size: Optional[int] = None,  # copies training batch size if None
            n_eval_iters: int = 1,
            eval_every: int = 200,
            sample_on_eval: bool = True,
            # optim
            train_lr: float = 1e-4,
            adam_betas: Tuple[float, float] = (0.9, 0.99),
            max_grad_norm: float = 1.,
            # logging
            results_folder: str = './results',
            ema_update_every: int = 10,
            ema_decay: float = 0.995,
            save_every_nth_eval: int = 1,
            score_on_save: bool = True,
            use_wandb: bool = False,
            save_config: bool = True,
            # accelerator
            amp: bool = False,
            mixed_precision_type: str = 'fp16',
            split_batches: bool = True,
    ):
        super().__init__()

        # accelerator

        self.accelerator = Accelerator(
            mixed_precision=mixed_precision_type if amp else 'no',
            dataloader_config=DataLoaderConfiguration(split_batches=split_batches),
        )

        # model

        self.model = diffusion_model

        # sampling and training hyperparameters

        self.eval_every = eval_every
        self.save_every = eval_every * save_every_nth_eval
        self.sample_on_eval = sample_on_eval
        self.score_on_save = score_on_save

        self.gradient_accumulate_every = gradient_accumulate_every
        assert (batch_size * gradient_accumulate_every) >= 16 or force_batch_size, \
            f'your effective batch size (train_batch_size x gradient_accumulate_every) should be at least 16 or above'

        self.train_num_steps = train_num_steps
        self.n_eval_iters = n_eval_iters

        self.max_grad_norm = max_grad_norm
        self.train_mask_unknown_ratings = train_mask_unknown_ratings

        self.data_holder = data_holder
        self.subgraph_sampling_args = subgraph_size, target_density
        if data_holder is not None:
            self.train_loader = _get_dataloader(
                dataset=data_holder.get_dataset(
                    subgraph_size=subgraph_size,
                    target_density=target_density,
                    train=True,
                ),
                batch_size=batch_size,
                accelerator=self.accelerator,
            )
            self.test_loader = _get_dataloader(
                dataset=data_holder.get_dataset(
                    subgraph_size=subgraph_size,
                    target_density=target_density,
                    train=False,
                ),
                batch_size=eval_batch_size or batch_size,
                accelerator=self.accelerator,
            )
        else:
            self.train_loader, self.test_loader = None, None

        # optimizer

        self.optim = Adam(diffusion_model.parameters(), lr=train_lr, betas=adam_betas)

        # for logging results in a folder periodically

        if self.accelerator.is_main_process:
            if str(self.device) in ('cpu', 'mps'):
                # skip ema b/c otherwise it throws annoying errors
                # I can't be bothered to fix right now
                no_op = lambda *args, **kwargs: None
                self.ema = type('Dummy', (), {
                    'ema_model': self.model,
                    'state_dict': no_op,
                    'load_state_dict': no_op,
                    'update': no_op,
                })
            else:
                self.ema = EMA(diffusion_model, beta=ema_decay, update_every=ema_update_every)
                self.ema.to(self.device)

        self.results_folder = Path(results_folder)
        self.results_folder.mkdir(exist_ok=True)

        # step counter state

        self.step = 0

        # prepare model, dataloader, optimizer with accelerator

        self.model, self.optim = _prepare(self.accelerator, (self.model, self.optim))

        # wandb

        self.logger = DataLogger(
            use_wandb=use_wandb,
            run_mode='train',
            run_config=get_kwargs(),
            initial_step=self.step,
            # ignore_numeric=tuple(METRIC_NAMES),
            save_config=save_config,
        )

    @property
    def device(self):
        return self.accelerator.device

    def save(self, milestone):
        if not self.accelerator.is_local_main_process:
            return

        data = {
            'step': self.step,
            'model': self.accelerator.get_state_dict(self.model),
            'opt': self.optim.state_dict(),
            'ema': self.ema.state_dict(),
            'scaler': self.accelerator.scaler.state_dict() if self.accelerator.scaler is not None else None
        }

        torch.save(data, str(self.results_folder / f'model-{milestone}.pt'))

    def load(self, milestone):
        accelerator = self.accelerator
        device = accelerator.device

        data = torch.load(str(self.results_folder / f'model-{milestone}.pt'), map_location=device)

        model = self.accelerator.unwrap_model(self.model)
        model.load_state_dict(data['model'])

        self.step = data['step']
        self.optim.load_state_dict(data['opt'])
        if self.accelerator.is_main_process:
            self.ema.load_state_dict(data["ema"])

        if 'version' in data:
            print(f"loading from version {data['version']}")

        if self.accelerator.scaler is not None and data['scaler'] is not None:
            self.accelerator.scaler.load_state_dict(data['scaler'])

    def train(self):
        if self.data_holder is None or self.train_loader is None or self.test_loader is None:
            raise ValueError("Cannot train without defined train_loader/test_loader")

        accelerator = self.accelerator
        device = accelerator.device

        with tqdm(
                initial=self.step,
                total=self.train_num_steps,
                disable=not accelerator.is_main_process,
                desc='training loop',
        ) as pbar:

            validation_loss = float('nan')
            training_loss_deque = deque(maxlen=100)  # average loss over this length

            while self.step < self.train_num_steps:

                total_loss = 0.
                self.model.train()

                for _ in range(self.gradient_accumulate_every):
                    data: RatingSubgraphData = _get(self.train_loader).to(device)
                    if not self.train_mask_unknown_ratings:
                        data.known_mask = None

                    with self.accelerator.autocast():
                        loss = self.model(data)
                        loss = loss / self.gradient_accumulate_every
                        total_loss += loss.item()

                    self.accelerator.backward(loss)

                self.logger.log(train_loss=total_loss)
                training_loss_deque.append(total_loss)

                accelerator.wait_for_everyone()
                accelerator.clip_grad_norm_(self.model.parameters(), self.max_grad_norm)

                self.optim.step()
                self.optim.zero_grad()

                accelerator.wait_for_everyone()

                self.step += 1
                if accelerator.is_main_process:
                    self.ema.update()

                    if self.step != 0 and self.step % self.save_every == 0:
                        self.save(self.step)

                        if self.score_on_save:
                            metrics = self.score(
                                predicting_ratings=True,  # TODO make configurable
                                n_samples=10,
                                do_inpainting_sampling=True,
                            )

                            loggable_metrics = {}
                            for metric, scores in metrics.items():
                                assert len(scores) == len(DEFAULT_TOP_K)
                                for top_k, score in zip(DEFAULT_TOP_K, scores):
                                    loggable_metrics[f"{metric}/{metric} @ K={top_k}"] = score
                            self.logger.log(loggable_metrics)

                    if self.step != 0 and self.step % self.eval_every == 0:
                        self.model.eval()

                        with torch.inference_mode():
                            val_loss = 0.
                            for _ in tqdm(range(self.n_eval_iters), desc="model eval"):
                                eval_data = _get(self.test_loader).to(device)
                                eval_data.ratings, eval_data.known_mask = torch.randn_like(eval_data.ratings) / 3, None

                                val_loss += self.model(eval_data).item()

                            validation_loss = val_loss / self.n_eval_iters
                            self.logger.log(validation_loss=validation_loss)

                            if self.sample_on_eval:
                                self.eval(
                                    rating_data=_get(self.test_loader).to(device),
                                    do_inpainting_sampling=True,
                                )

                train_loss = sum(training_loss_deque) / len(training_loss_deque)

                self.logger.log(step=self.step, commit=True)
                pbar.set_postfix(train_loss=f"{train_loss:.4f}", valid_loss=f"{validation_loss:.4f}")
                pbar.update(1)

        accelerator.print('training complete')

    def eval(
            self,
            milestone: Optional[int] = None,
            rating_data: Optional[RatingSubgraphData] = None,
            tiled_sampling: bool = False,
            batch_size: Optional[int] = None,
            subgraph_size: Optional[Union[int, Tuple[int, int]]] = None,
            do_inpainting_sampling: bool = False,
            silence_inner_tqdm: bool = False,
            save_sampled_graph: bool = True,
    ) -> torch.Tensor:
        if milestone is not None:
            self.load(milestone)

        if rating_data is None:
            sampling_subgraph_size, sampling_target_density = self.subgraph_sampling_args

            rating_data = self.data_holder.get_subgraph(
                subgraph_size=sampling_subgraph_size,
                target_density=sampling_target_density,
                return_train_edges=True,
                return_test_edges=False,
            )

        rating_data, _ = rating_data.with_batching()

        rating_data = rating_data.to(self.device)
        inpainting_data = (rating_data.ratings, rating_data.known_mask) if do_inpainting_sampling else None
        rating_data.ratings, rating_data.known_mask = torch.randn_like(rating_data.ratings) / 3, None

        if tiled_sampling:
            sampled_graph = self.model.sample_tiled(
                rating_data=rating_data,
                subgraph_size=subgraph_size,
                max_batch_size=batch_size,
                inpainting_data=inpainting_data,
                silence_inner_tqdm=silence_inner_tqdm,
            )
        else:
            sampled_graph = self.model.sample(
                rating_data=rating_data,
                inpainting_data=inpainting_data,
                silence_inner_tqdm=silence_inner_tqdm,
            )

        sampled_graph = sampled_graph.detach().cpu()
        if save_sampled_graph:
            np.save(str(self.results_folder / f"eval-sample-{milestone}.npy"), sampled_graph.numpy())

        return sampled_graph

    def score(
            self,
            milestone: Optional[int] = None,
            predicting_ratings: bool = False,
            predicting_interactions: bool = False,
            n_samples: int = 1,  # number of samples to average over
            tiled_sampling: bool = False,
            batch_size: Optional[int] = None,
            subgraph_size: Optional[Union[int, Tuple[int, int]]] = None,
            do_inpainting_sampling: bool = False,
            silence_inner_tqdm: bool = False,
    ) -> Dict[str, np.ndarray]:
        if milestone is not None:
            self.load(milestone)

        sampling_subgraph_size, sampling_target_density = self.subgraph_sampling_args

        # get metric compute fn
        if np.sum([predicting_ratings, predicting_interactions]) != 1:
            raise ValueError(
                "Must specify scoring using exactly one metric: "
                "'predicting_ratings' or 'predicting_interactions'"
            )
        if predicting_ratings:
            # get transformation object, if possible
            for field_name in (
                    'ratings_transform',
                    'rating_transform',
            ):
                transform = getattr(self.data_holder, field_name, None)
                if transform is not None:
                    break

            # build metric compute fn
            compute_metrics = partial(
                compute_metrics_from_ratings,
                rating_transform=transform,
            )
        else:
            compute_metrics = compute_metrics_from_interactions

        # compute metrics
        metrics = {metric: [] for metric in METRIC_NAMES}
        for _ in tqdm(range(n_samples), desc="score model -- compute average metrics"):
            # generate data for sampling
            user_indices, product_indices = self.data_holder.get_subgraph_indices(
                subgraph_size=sampling_subgraph_size,
                target_density=sampling_target_density,
            )
            rating_data_train = self.data_holder.slice_subgraph(
                user_indices=user_indices,
                product_indices=product_indices,
                return_train_edges=True,
                return_test_edges=False,
            )
            rating_data_test = self.data_holder.slice_subgraph(
                user_indices=user_indices,
                product_indices=product_indices,
                return_train_edges=False,
                return_test_edges=True,
            )
            denoised_graph = self.eval(
                rating_data=rating_data_train.clone(),
                milestone=None,  # already loaded at top of function
                do_inpainting_sampling=do_inpainting_sampling,
                tiled_sampling=tiled_sampling,
                batch_size=batch_size,
                subgraph_size=subgraph_size,
                silence_inner_tqdm=silence_inner_tqdm,
                save_sampled_graph=False,
            )

            # compute metrics
            computed_metrics = compute_metrics(
                predicted_graph=denoised_graph,
                train_rating_data=rating_data_train,
                test_rating_data=rating_data_test,
            )

            # collate metrics
            assert len(computed_metrics) == len(METRIC_NAMES) == len(metrics)
            for key, stats in computed_metrics.items():
                metrics[key].append(stats)

        # compute average metrics
        return {
            key: sum(values) / len(values)
            for key, values in metrics.items()
        }
