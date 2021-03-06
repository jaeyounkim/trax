# coding=utf-8
# Copyright 2020 The Trax Authors.
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

# Lint as: python3
"""Classes for RL training in Trax."""

import functools

from trax import layers as tl
from trax import lr_schedules as lr
from trax import supervised
from trax.math import numpy as jnp
from trax.rl import actor_critic
from trax.rl import distributions
from trax.rl import rl_layers
from trax.rl import training as rl_training


# pylint: disable=g-long-lambda
class ActorCriticJointTrainer(rl_training.RLTrainer):
  """Trains a joint policy-and-value model using actor-critic methods."""

  def __init__(self, task, joint_model=None,
               optimizer=None, lr_schedule=lr.MultifactorSchedule,
               batch_size=64, train_steps_per_epoch=500,
               supervised_evals_per_epoch=1, supervised_eval_steps=1,
               collect_per_epoch=50, max_slice_length=1,
               normalize_advantages=True, output_dir=None):
    """Configures the joint trainer.

    Args:
      task: RLTask instance, which defines the environment to train on.
      joint_model: Trax layer, representing the joint policy and value model.
      optimizer: the optimizer to use to train the joint model.
      lr_schedule: learning rate schedule to use to train the joint model/.
      batch_size: batch size used to train the joint model.
      train_steps_per_epoch: how long to train the joint model in each RL epoch.
      supervised_evals_per_epoch: number of value trainer evaluations per RL
          epoch - only affects metric reporting.
      supervised_eval_steps: number of value trainer steps per evaluation -
          only affects metric reporting.
      collect_per_epoch: how many trajectories to collect per epoch.
      max_slice_length: the maximum length of trajectory slices to use.
      normalize_advantages: if True, then normalize advantages - currently
          implemented only in PPO.
      output_dir: Path telling where to save outputs (evals and checkpoints).
    """
    super(ActorCriticJointTrainer, self).__init__(
        task, collect_per_epoch=collect_per_epoch, output_dir=output_dir)
    self._batch_size = batch_size
    self._train_steps_per_epoch = train_steps_per_epoch
    self._supervised_evals_per_epoch = supervised_evals_per_epoch
    self._supervised_eval_steps = supervised_eval_steps
    self._collect_per_epoch = collect_per_epoch
    self._max_slice_length = max_slice_length
    self._policy_dist = distributions.create_distribution(task.action_space)
    self._lr_schedule = lr_schedule
    self._optimizer = optimizer
    self._normalize_advantages = normalize_advantages

    # Inputs to the joint model are produced by self.batches_stream.
    self._inputs = supervised.Inputs(
        train_stream=lambda _: self.batches_stream())

    self._joint_model = functools.partial(
        joint_model,
        policy_distribution=self._policy_dist,
    )

    # This is the joint Trainer that will be used to train the policy model.
    # * inputs to the trainer come from self.batches_stream
    # * outputs are passed to self._joint_loss
    self._trainer = supervised.Trainer(
        model=self._joint_model,
        optimizer=self._optimizer,
        lr_schedule=self._lr_schedule,
        loss_fn=self.joint_loss,
        inputs=self._inputs,
        output_dir=output_dir,
        metrics={'joint_loss': self.joint_loss,
                 'advantage_mean': self.advantage_mean,
                 'advantage_norm': self.advantage_norm,
                 'value_loss': self.value_loss,
                 'explained_variance': self.explained_variance,
                 'log_probs_mean': self.log_probs_mean,
                 'preferred_move': self.preferred_move})
    self._eval_model = self._joint_model(mode='eval')
    example_batch = next(self.batches_stream())
    self._eval_model.init(example_batch)

  def batches_stream(self):
    """Use self.task to create inputs to the policy model."""
    return NotImplementedError

  @property
  def joint_loss(self):
    """Joint policy and value loss layer."""
    return NotImplementedError

  @property
  def advantage_mean(self):
    """Mean of advantages."""
    def AdvantageMean(values, returns):
      """Definition of the mean of advantages."""
      advantages = returns - values
      return jnp.mean(advantages)
    layer = tl.Fn(
        lambda dist_inputs, values, returns: AdvantageMean(values, returns),
        n_in=3,
        n_out=1)
    return lambda **unused_kwargs: layer

  @property
  def advantage_norm(self):
    """Mean of advantages."""
    def AdvantageNorm(values, returns):
      """Definition of the mean of advantages."""
      advantages = returns - values
      return jnp.linalg.norm(advantages)
    layer = tl.Fn(
        lambda dist_inputs, values, returns: AdvantageNorm(values, returns),
        n_in=3,
        n_out=1)
    return lambda **unused_kwargs: layer

  @property
  def value_loss(self):
    """Value loss - so far generic for all A2C."""
    layer = tl.Fn(lambda dist_inputs, values, returns: rl_layers.ValueLoss(
        values, returns, self._value_loss_coeff),
                  n_in=3, n_out=1)
    return lambda **unused_kwargs: layer

  @property
  def explained_variance(self):
    """Explained variance metric."""
    layer = tl.Fn(rl_layers.ExplainedVariance,
                  n_in=2, n_out=1)
    return lambda **unused_kwargs: layer

  @property
  def log_probs_mean(self):
    """Mean of log_probs aka dist_inputs."""
    layer = tl.Fn(lambda dist_inputs, values: jnp.mean(dist_inputs),
                  n_in=2, n_out=1)
    return lambda **unused_kwargs: layer

  @property
  def preferred_move(self):
    """Preferred move - the mean of selected moves."""
    layer = tl.Fn(lambda dist_inputs, values: rl_layers.PreferredMove(
        dist_inputs, self._policy_dist.sample), n_in=2, n_out=1)
    return lambda **unused_kwargs: layer

  def policy(self, trajectory):
    """Chooses an action to play after a trajectory."""
    model = self._eval_model
    model.weights = self._trainer.model_weights
    # The two lines below along with the copying
    # before return make the TPU happy
    tr_slice = trajectory[-self._max_slice_length:]
    trajectory_np = tr_slice.to_np(timestep_to_np=self.task.timestep_to_np)
    # Add batch dimension to trajectory_np and run the model.
    pred = model(trajectory_np.observations[None, ...], n_accelerators=1)[0]
    # Pick element 0 from the batch (the only one), last (current) timestep.
    pred = pred[0, -1, :]
    sample = self._policy_dist.sample(pred)
    log_prob = self._policy_dist.log_prob(pred, sample)
    return (sample.copy(), log_prob.copy())

  def train_epoch(self):
    """Trains RL for one epoch."""
    n_evals = rl_training.remaining_evals(
        self._trainer.step,
        self._epoch,
        self._train_steps_per_epoch,
        self._supervised_evals_per_epoch)
    for _ in range(n_evals):
      self._trainer.train_epoch(
          self._train_steps_per_epoch // self._supervised_evals_per_epoch,
          self._supervised_eval_steps)


class PPOJointTrainer(ActorCriticJointTrainer):
  """The Proximal Policy Optimization Algorithm aka PPO.

  Trains policy and value models using the PPO algortithm.
  """

  # TODO(henrykm): make on_policy more generic
  # (currently epochs are passed manually)
  on_policy = True

  def __init__(self, task, epsilon=0.2, value_loss_coeff=0.1,
               entropy_coeff=0.01, **kwargs):
    """Configures the PPO Trainer."""
    self._epsilon = epsilon
    self._value_loss_coeff = value_loss_coeff
    self._entropy_coeff = entropy_coeff
    super(PPOJointTrainer, self).__init__(task, **kwargs)
    self._trainer = supervised.Trainer(
        model=self._joint_model,
        optimizer=self._optimizer,
        lr_schedule=self._lr_schedule,
        loss_fn=self.joint_loss,
        inputs=self._inputs,
        output_dir=self._output_dir,
        metrics={'joint_loss': self.joint_loss,
                 'advantage_mean': self.advantage_mean,
                 'advantage_norm': self.advantage_norm,
                 'value_loss': self.value_loss,
                 'explained_variance': self.explained_variance,
                 'log_probs_mean': self.log_probs_mean,
                 'entropy_loss': self.entropy_loss,
                 'probs_ratio_mean': self.probs_ratio_mean,
                 'unclipped_objective_mean': self.unclipped_objective_mean,
                 'clipped_objective_mean': self.clipped_objective_mean,
                 'ppo_objective_mean': self.ppo_objective_mean,
                 'clip_fraction': self.clip_fraction,
                 'approximate_kl_divergence': self.approximate_kl_divergence,
                 'preferred_move': self.preferred_move})

  def batches_stream(self):
    """Use the RLTask self._task to create inputs to the value model."""
    for np_trajectory in self._task.trajectory_batch_stream(
        self._batch_size, max_slice_length=self._max_slice_length, epochs=[-1]):
      # Insert an extra depth dimension, so the target shape is consistent with
      # the network output shape.
      yield (np_trajectory.observations,         # Inputs to the value model.
             np_trajectory.returns[:, :, None],
             np_trajectory.actions,
             np_trajectory.log_probs,
             np_trajectory.mask)

  @property
  def joint_loss(self):
    """Joint policy and value loss."""
    def PPOJointLoss(dist_inputs, values, returns, actions, old_log_probs,
                     mask):
      """Definition of the Proximal Policy Optimization loss."""
      del mask  # TODO(lukaszkaiser): make PPO work with Transformer

      ppo_objective = rl_layers.PPOObjective(
          dist_inputs, values, returns, actions, old_log_probs,
          log_prob_fun=self._policy_dist.log_prob,
          epsilon=self._epsilon,
          normalize_advantages=self._normalize_advantages)

      entropy_loss = rl_layers.EntropyLoss(
          dist_inputs, actions,
          log_prob_fun=self._policy_dist.log_prob,
          entropy_coeff=self._entropy_coeff,
          entropy_fun=self._policy_dist.entropy)

      l2_value_loss = rl_layers.ValueLoss(
          values, returns, value_loss_coeff=self._value_loss_coeff)

      return -ppo_objective.mean() + l2_value_loss - entropy_loss

    return lambda **unused_kwargs: tl.Fn(PPOJointLoss, n_in=6, n_out=1)

  @property
  def probs_ratio_mean(self):
    """Joint policy and value loss layer."""
    def ProbsRatioMean(dist_inputs, actions, old_log_probs):
      """Probability Ratio Mean from the PPO algorithm."""
      probs_ratio = rl_layers.ProbsRatio(
          dist_inputs, actions, old_log_probs,
          log_prob_fun=self._policy_dist.log_prob)
      return jnp.mean(probs_ratio)

    layer = tl.Fn(
        lambda dist_inputs, values, returns, actions, old_log_probs:
        ProbsRatioMean(dist_inputs, actions, old_log_probs),
        n_in=5,
        n_out=1)
    return lambda **unused_kwargs: layer

  @property
  def clip_fraction(self):
    """Joint policy and value loss layer."""
    def ClipFraction(dist_inputs, actions, old_log_probs):
      """Probability Ratio Mean from the PPO algorithm."""
      probs_ratio = rl_layers.ProbsRatio(
          dist_inputs, actions, old_log_probs,
          log_prob_fun=self._policy_dist.log_prob)
      return jnp.mean(jnp.abs(probs_ratio - 1) > self._epsilon)

    layer = tl.Fn(
        lambda dist_inputs, values, returns, actions, old_log_probs:
        ClipFraction(dist_inputs, actions, old_log_probs),
        n_in=5,
        n_out=1)
    return lambda **unusd_kwargs: layer

  @property
  def entropy_loss(self):
    """Entropy layer."""
    layer = tl.Fn(
        lambda dist_inputs, values, returns, actions:
        rl_layers.EntropyLoss(
            dist_inputs, actions, log_prob_fun=self._policy_dist.log_prob,
            entropy_coeff=self._entropy_coeff,
            entropy_fun=self._policy_dist.entropy),
        n_in=4, n_out=1)
    return lambda **unusd_kwargs: layer

  @property
  def approximate_kl_divergence(self):
    """Entropy layer."""
    layer = tl.Fn(
        lambda dist_inputs, actions, old_log_probs:
        rl_layers.ApproximateKLDivergence(
            dist_inputs,
            actions,
            old_log_probs,
            log_prob_fun=self._policy_dist.log_prob),
        n_in=3,
        n_out=1)
    return lambda **unused_kwargs: layer

  @property
  def unclipped_objective_mean(self):
    def UnclippedObjectiveMean(dist_inputs, values,
                               returns, actions, old_log_probs):
      """Unclipped objective Mean from the PPO algorithm."""
      advantages = returns - values
      probs_ratio = rl_layers.ProbsRatio(
          dist_inputs, actions, old_log_probs,
          log_prob_fun=self._policy_dist.log_prob)
      unclipped_objective = rl_layers.UnclippedObjective(
          probs_ratio, advantages)
      return jnp.mean(unclipped_objective)

    return lambda **unused_kwargs: tl.Fn(
        UnclippedObjectiveMean, n_in=5, n_out=1)

  @property
  def clipped_objective_mean(self):
    def ClippedObjectiveMean(
        dist_inputs, values, returns, actions, old_log_probs):
      """Clipped objective from the PPO algorithm."""
      advantages = returns - values
      probs_ratio = rl_layers.ProbsRatio(
          dist_inputs, actions, old_log_probs,
          log_prob_fun=self._policy_dist.log_prob)
      clipped_objective = rl_layers.ClippedObjective(
          probs_ratio, advantages, epsilon=self._epsilon)
      return jnp.mean(clipped_objective)

    return lambda **unused_kwargs: tl.Fn(ClippedObjectiveMean, n_in=5, n_out=1)

  @property
  def ppo_objective(self):
    """PPO objective with local parameters."""
    layer = tl.Fn(
        lambda dist_inputs, values, returns, actions, old_log_probs:
        rl_layers.PPOObjective(
            dist_inputs, values, returns, actions, old_log_probs,
            log_prob_fun=self._policy_dist.log_prob,
            epsilon=self._epsilon,
            normalize_advantages=self._normalize_advantages),
        n_in=5, n_out=1)
    return lambda **unused_kwargs: layer

  @property
  def ppo_objective_mean(self):
    """PPO objective mean."""
    def PPOObjectiveMean(dist_inputs, values, returns, actions, old_log_probs):
      """Clipped objective from the PPO algorithm."""
      ppo_objective = rl_layers.PPOObjective(
          dist_inputs, values, returns, actions, old_log_probs,
          log_prob_fun=self._policy_dist.log_prob,
          epsilon=self._epsilon,
          normalize_advantages=self._normalize_advantages)
      return jnp.mean(ppo_objective)
    return lambda **unused_kwargs: tl.Fn(PPOObjectiveMean, n_in=5, n_out=1)


class AWRJointTrainer(ActorCriticJointTrainer):
  """Trains a joint policy-and-value model using AWR."""

  # TODO(henrykm): value_loss_coeff looks like a common parameter
  def __init__(self, task, value_loss_coeff=0.1, beta=1.0, w_max=20.0,
               **kwargs):
    """Configures the joint AWR Trainer."""
    self._beta = beta
    self._w_max = w_max
    self._value_loss_coeff = value_loss_coeff
    super(AWRJointTrainer, self).__init__(task, **kwargs)

  def batches_stream(self):
    """Use the RLTask self._task to create inputs to the value model."""
    for np_trajectory in self._task.trajectory_batch_stream(
        self._batch_size, max_slice_length=self._max_slice_length):
      # Insert an extra depth dimension, so the target shape is consistent with
      # the network output shape.
      yield (np_trajectory.observations,         # Inputs to the value model.
             np_trajectory.returns[:, :, None],  # Targets: regress to returns.
             np_trajectory.actions,              # Policy targets: actions.
             np_trajectory.mask)                 # Padding mask.

  @property
  def joint_loss(self):
    """Joint policy and value loss."""
    @tl.layer(n_in=5, n_out=1)
    def AWRJointLoss(x, **unused_kwargs):  # pylint: disable=invalid-name
      preds, values, returns, actions, mask = x
      advantages = jnp.squeeze(returns - values, axis=-1)
      logps = self._policy_dist.log_prob(preds, actions)
      awr_loss = actor_critic.AWRLoss(beta=self._beta, w_max=self._w_max)(
          (logps, advantages, jnp.zeros_like(logps), mask))
      l2_value_loss = jnp.mean((returns - values)**2) * self._value_loss_coeff
      return awr_loss + l2_value_loss
    return AWRJointLoss
