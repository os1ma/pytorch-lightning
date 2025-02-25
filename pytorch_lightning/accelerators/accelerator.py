# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import contextlib
from abc import abstractmethod
from typing import Any, Callable, Dict, Generator, List, Optional, Union

import torch
from torch import Tensor
from torch.cuda.amp import GradScaler
from torch.nn import Module
from torch.optim import Optimizer

import pytorch_lightning as pl
from pytorch_lightning.plugins.precision import ApexMixedPrecisionPlugin, NativeMixedPrecisionPlugin, PrecisionPlugin
from pytorch_lightning.plugins.training_type import DataParallelPlugin, TrainingTypePlugin
from pytorch_lightning.trainer.states import TrainerFn
from pytorch_lightning.utilities.apply_func import apply_to_collection, move_data_to_device
from pytorch_lightning.utilities.enums import AMPType, LightningEnum
from pytorch_lightning.utilities.types import STEP_OUTPUT


class Accelerator:
    """The Accelerator Base Class. An Accelerator is meant to deal with one type of Hardware.

    Currently there are accelerators for:

    - CPU
    - GPU
    - TPU
    - IPU

    Each Accelerator gets two plugins upon initialization:
    One to handle differences from the training routine and one to handle different precisions.
    """

    def __init__(self, precision_plugin: PrecisionPlugin, training_type_plugin: TrainingTypePlugin) -> None:
        """
        Args:
            precision_plugin: the plugin to handle precision-specific parts
            training_type_plugin: the plugin to handle different training routines
        """
        self.precision_plugin = precision_plugin
        self.training_type_plugin = training_type_plugin

        self.optimizers: List = []
        self.lr_schedulers: List = []
        self.optimizer_frequencies: List = []

    def setup_environment(self) -> None:
        """Setup any processes or distributed connections.

        This is called before the LightningModule/DataModule setup hook which allows the user to access the accelerator
        environment before setup is complete.
        """
        self.training_type_plugin.setup_environment()

    def setup(self, trainer: "pl.Trainer") -> None:
        """Setup plugins for the trainer fit and creates optimizers.

        Args:
            trainer: the trainer instance
        """
        self.setup_training_type_plugin()
        if not self.training_type_plugin.setup_optimizers_in_pre_dispatch:
            self.setup_optimizers(trainer)
        self.setup_precision_plugin()

    def pre_dispatch(self, trainer: "pl.Trainer") -> None:
        """Hook to do something before the training/evaluation/prediction starts."""
        self._move_optimizer_state()

        self.training_type_plugin.pre_dispatch()
        if self.training_type_plugin.setup_optimizers_in_pre_dispatch:
            self.setup_optimizers(trainer)

        self.precision_plugin.pre_dispatch()

    def _move_optimizer_state(self, device: Optional[torch.device] = None) -> None:
        """Moves the state of the optimizers to the GPU if needed."""
        device = device or self.root_device
        for opt in self.optimizers:
            for p, v in opt.state.items():
                opt.state[p] = apply_to_collection(v, torch.Tensor, move_data_to_device, device)

    def dispatch(self, trainer: "pl.Trainer") -> None:
        """Hook to do something before the training/evaluation/prediction starts."""
        self.training_type_plugin.dispatch(trainer)
        self.precision_plugin.dispatch(trainer)

    def post_dispatch(self, trainer: "pl.Trainer") -> None:
        """Hook to do something after the training/evaluation/prediction starts."""
        self.training_type_plugin.post_dispatch(trainer)
        self.precision_plugin.post_dispatch()

    @property
    def model(self) -> Module:
        """Returns the model.

        This can also be a wrapped LightningModule. For retrieving the pure LightningModule use
        :attr:`Accelerator.lightning_module`
        """
        return self.training_type_plugin.model

    @model.setter
    def model(self, new_model: Module) -> None:
        self.training_type_plugin.model = new_model

    @property
    def lightning_module(self) -> "pl.LightningModule":
        """Returns the pure LightningModule.

        To get the potentially wrapped model use :attr:`Accelerator.model`
        """
        return self.training_type_plugin.lightning_module

    @property
    def root_device(self) -> torch.device:
        """Returns the root device."""
        return self.training_type_plugin.root_device

    def teardown(self) -> None:
        """This method is called to teardown the training process.

        It is the right place to release memory and free other resources.
        """
        self.training_type_plugin.teardown()

    def batch_to_device(self, batch: Any, device: Optional[torch.device] = None, dataloader_idx: int = 0) -> Any:
        """Moves the batch to the correct device. The returned batch is of the same type as the input batch, just
        having all tensors on the correct device.

        Args:
            batch: The batch of samples to move to the correct device
            device: The target device
            dataloader_idx: The index of the dataloader to which the batch belongs.
        """
        model = self.lightning_module
        device = device or self.root_device

        if model is not None and not isinstance(self.training_type_plugin, DataParallelPlugin):
            # no need to transfer batch to device in DP mode
            return model._apply_batch_transfer_handler(batch, device=device, dataloader_idx=dataloader_idx)

        return move_data_to_device(batch, device)

    def training_step(self, step_kwargs: Dict[str, Union[Any, int]]) -> STEP_OUTPUT:
        """The actual training step.

        See :meth:`~pytorch_lightning.core.lightning.LightningModule.training_step` for more details
        """
        with self.precision_plugin.train_step_context():
            return self.training_type_plugin.training_step(*step_kwargs.values())

    def validation_step(self, step_kwargs: Dict[str, Union[Any, int]]) -> Optional[STEP_OUTPUT]:
        """The actual validation step.

        See :meth:`~pytorch_lightning.core.lightning.LightningModule.validation_step` for more details
        """
        with self.precision_plugin.val_step_context():
            return self.training_type_plugin.validation_step(*step_kwargs.values())

    def test_step(self, step_kwargs: Dict[str, Union[Any, int]]) -> Optional[STEP_OUTPUT]:
        """The actual test step.

        See :meth:`~pytorch_lightning.core.lightning.LightningModule.test_step` for more details
        """
        with self.precision_plugin.test_step_context():
            return self.training_type_plugin.test_step(*step_kwargs.values())

    def predict_step(self, step_kwargs: Dict[str, Union[Any, int]]) -> STEP_OUTPUT:
        """The actual predict step.

        See :meth:`~pytorch_lightning.core.lightning.LightningModule.predict_step` for more details
        """
        with self.precision_plugin.predict_step_context():
            return self.training_type_plugin.predict_step(*step_kwargs.values())

    def backward(self, closure_loss: Tensor, *args: Any, **kwargs: Any) -> Tensor:
        """Forwards backward-calls to the precision plugin.

        Args:
            closure_loss: a tensor holding the loss value to backpropagate
        """
        self.training_type_plugin.pre_backward(closure_loss)
        closure_loss = self.precision_plugin.pre_backward(self.lightning_module, closure_loss)

        self.precision_plugin.backward(self.lightning_module, closure_loss, *args, **kwargs)

        closure_loss = self.precision_plugin.post_backward(self.lightning_module, closure_loss)
        self.training_type_plugin.post_backward(closure_loss)

        return closure_loss

    def optimizer_step(
        self,
        optimizer: Optimizer,
        opt_idx: int,
        closure: Callable[[], Any],
        model: Optional[Union["pl.LightningModule", Module]] = None,
        **kwargs: Any
    ) -> None:
        """performs the actual optimizer step.

        Args:
            optimizer: the optimizer performing the step
            opt_idx: index of the current optimizer
            closure: closure calculating the loss value
            model: reference to the model, optionally defining optimizer step related hooks
            **kwargs: Any extra arguments to ``optimizer.step``
        """
        model = model or self.lightning_module
        self.precision_plugin.optimizer_step(model, optimizer, opt_idx, closure, **kwargs)

    def optimizer_zero_grad(self, current_epoch: int, batch_idx: int, optimizer: Optimizer, opt_idx: int) -> None:
        """Zeros all model parameter's gradients."""
        model_ref = self.lightning_module
        model_ref.optimizer_zero_grad(current_epoch, batch_idx, optimizer, opt_idx)

    def setup_optimizers(self, trainer: "pl.Trainer") -> None:
        """Creates optimizers and schedulers.

        Args:
            trainer: the Trainer, these optimizers should be connected to
        """
        if trainer.state.fn not in (TrainerFn.FITTING, TrainerFn.TUNING):
            return
        optimizers, lr_schedulers, optimizer_frequencies = self.training_type_plugin.init_optimizers(
            trainer=trainer, model=self.lightning_module
        )
        self.optimizers = optimizers
        self.lr_schedulers = lr_schedulers
        self.optimizer_frequencies = optimizer_frequencies

    def setup_training_type_plugin(self) -> None:
        """Attaches the training type plugin to the accelerator."""
        self.training_type_plugin.setup()

    def setup_precision_plugin(self) -> None:
        """Attaches the precision plugin to the accelerator."""
        model, optimizers, schedulers = self.precision_plugin.connect(self.model, self.optimizers, self.lr_schedulers)
        self.model = model
        self.optimizers = optimizers
        self.lr_schedulers = schedulers

    @property
    def amp_backend(self) -> Optional[LightningEnum]:
        if isinstance(self.precision_plugin, ApexMixedPrecisionPlugin):
            return AMPType.APEX
        if isinstance(self.precision_plugin, NativeMixedPrecisionPlugin):
            return AMPType.NATIVE
        return None

    @property
    def precision(self) -> Union[str, int]:
        return self.precision_plugin.precision

    @property
    def scaler(self) -> Optional["GradScaler"]:
        return getattr(self.precision_plugin, "scaler", None)

    def optimizer_state(self, optimizer: Optimizer) -> Dict[str, Tensor]:
        """Returns state of an optimizer.

        Allows for syncing/collating optimizer state from processes in custom plugins.
        """
        return getattr(self.training_type_plugin, "optimizer_state", lambda x: x.state_dict())(optimizer)

    @contextlib.contextmanager
    def model_sharded_context(self) -> Generator[None, None, None]:
        """Provide hook to create modules in a distributed aware context. This is useful for when we'd like to.

        shard the model instantly - useful for extremely large models. Can save memory and
        initialization time.

        Returns:
            Model parallel context.
        """
        with self.training_type_plugin.model_sharded_context():
            yield

    def get_device_stats(self, device: Union[str, torch.device]) -> Dict[str, Any]:
        """Gets stats for a given device.

        Args:
            device: device for which to get stats

        Returns:
            Dictionary of device stats
        """
        raise NotImplementedError

    def on_train_start(self) -> None:
        """Called when train begins."""
        return self.training_type_plugin.on_train_start()

    @staticmethod
    @abstractmethod
    def auto_device_count() -> int:
        """Get the devices when set to auto."""
