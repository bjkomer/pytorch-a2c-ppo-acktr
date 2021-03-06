import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from rl_algos.utils import one_hot


class PPO(object):
    def __init__(self,
                 actor_critic,
                 clip_param,
                 ppo_epoch,
                 num_mini_batch,
                 value_loss_coef,
                 entropy_coef,
                 lr=None,
                 eps=None,
                 max_grad_norm=None,
                 use_clipped_value_loss=True):

        self.actor_critic = actor_critic

        self.clip_param = clip_param
        self.ppo_epoch = ppo_epoch
        self.num_mini_batch = num_mini_batch

        self.value_loss_coef = value_loss_coef
        self.entropy_coef = entropy_coef

        self.max_grad_norm = max_grad_norm
        self.use_clipped_value_loss = use_clipped_value_loss

        self.optimizer = optim.Adam(actor_critic.parameters(), lr=lr, eps=eps)

    def update(self, rollouts):
        advantages = rollouts.returns[:-1] - rollouts.value_preds[:-1]
        advantages = (advantages - advantages.mean()) / (
            advantages.std() + 1e-5)

        value_loss_epoch = 0
        action_loss_epoch = 0
        dist_entropy_epoch = 0

        for e in range(self.ppo_epoch):
            if self.actor_critic.is_recurrent:
                data_generator = rollouts.recurrent_generator(
                    advantages, self.num_mini_batch)
            else:
                data_generator = rollouts.feed_forward_generator(
                    advantages, self.num_mini_batch)

            for sample in data_generator:
                obs_batch, recurrent_hidden_states_batch, actions_batch, \
                   value_preds_batch, return_batch, masks_batch, old_action_log_probs_batch, \
                        adv_targ = sample

                # Reshape to do in a single forward pass for all steps
                values, action_log_probs, dist_entropy, _ = self.actor_critic.evaluate_actions(
                    obs_batch, recurrent_hidden_states_batch,
                    masks_batch, actions_batch)

                ratio = torch.exp(action_log_probs - old_action_log_probs_batch)
                surr1 = ratio * adv_targ
                surr2 = torch.clamp(ratio, 1.0 - self.clip_param,
                                           1.0 + self.clip_param) * adv_targ
                action_loss = -torch.min(surr1, surr2).mean()

                if self.use_clipped_value_loss:
                    value_pred_clipped = value_preds_batch + \
                        (values - value_preds_batch).clamp(-self.clip_param, self.clip_param)
                    value_losses = (values - return_batch).pow(2)
                    value_losses_clipped = (value_pred_clipped - return_batch).pow(2)
                    value_loss = 0.5 * torch.max(value_losses, value_losses_clipped).mean()
                else:
                    value_loss = 0.5 * (return_batch - values).pow(2).mean()

                self.optimizer.zero_grad()
                (value_loss * self.value_loss_coef + action_loss -
                 dist_entropy * self.entropy_coef).backward()
                nn.utils.clip_grad_norm_(self.actor_critic.parameters(),
                                         self.max_grad_norm)
                self.optimizer.step()

                value_loss_epoch += value_loss.item()
                action_loss_epoch += action_loss.item()
                dist_entropy_epoch += dist_entropy.item()

        num_updates = self.ppo_epoch * self.num_mini_batch

        value_loss_epoch /= num_updates
        action_loss_epoch /= num_updates
        dist_entropy_epoch /= num_updates

        return value_loss_epoch, action_loss_epoch, dist_entropy_epoch


class CuriosityPPO(PPO):
    def __init__(self,
                 # feature_size,
                 # action_size,
                 # state_size,
                 forward_model,
                 inverse_model,
                 feature_encoder,
                 lam_pol=0.5,  # TODO: get correct default
                 forward_loss_weight=0.2,
                 **kwargs):

        self.lam_pol = lam_pol
        self.forward_loss_weight = forward_loss_weight

        self.forward_model = forward_model
        self.inverse_model = inverse_model
        self.feature_encoder = feature_encoder

        # self.feature_size = feature_size
        # self.action_size = action_size
        # self.state_size = state_size
        #
        # self.forward_model = ForwardModel(feature_size, action_size)
        # self.inverse_model = InverseModel(feature_size, action_size)
        # self.feature_encoder = FeatureEncoder(state_size, feature_size)

        self.forward_loss_fn = nn.CrossEntropyLoss()

        super(CuriosityPPO, self).__init__(**kwargs)

    def update(self, rollouts):
        advantages = rollouts.returns[:-1] - rollouts.value_preds[:-1]
        advantages = (advantages - advantages.mean()) / (
            advantages.std() + 1e-5)

        value_loss_epoch = 0
        action_loss_epoch = 0
        dist_entropy_epoch = 0

        for e in range(self.ppo_epoch):
            if self.actor_critic.is_recurrent:
                data_generator = rollouts.recurrent_generator(
                    advantages, self.num_mini_batch, curiosity=True)
            else:
                data_generator = rollouts.feed_forward_generator(
                    advantages, self.num_mini_batch, curiosity=True)

            for sample in data_generator:
                obs_batch, recurrent_hidden_states_batch, actions_batch, \
                   value_preds_batch, return_batch, masks_batch, old_action_log_probs_batch, \
                        adv_targ, prev_obs_batch = sample

                # Reshape to do in a single forward pass for all steps
                values, action_log_probs, dist_entropy, _ = self.actor_critic.evaluate_actions(
                    obs_batch, recurrent_hidden_states_batch,
                    masks_batch, actions_batch)

                features_batch = self.feature_encoder(obs_batch)
                prev_features_batch = self.feature_encoder(prev_obs_batch)
                action_pred_batch = self.inverse_model(prev_features_batch, features_batch)
                features_pred_batch = self.forward_model(prev_features_batch,
                                                         one_hot(actions_batch, max_val=self.forward_model.action_size))

                ratio = torch.exp(action_log_probs - old_action_log_probs_batch)
                surr1 = ratio * adv_targ
                surr2 = torch.clamp(ratio, 1.0 - self.clip_param,
                                           1.0 + self.clip_param) * adv_targ
                action_loss = -torch.min(surr1, surr2).mean()

                if self.use_clipped_value_loss:
                    value_pred_clipped = value_preds_batch + \
                        (values - value_preds_batch).clamp(-self.clip_param, self.clip_param)
                    value_losses = (values - return_batch).pow(2)
                    value_losses_clipped = (value_pred_clipped - return_batch).pow(2)
                    value_loss = 0.5 * torch.max(value_losses, value_losses_clipped).mean()
                else:
                    value_loss = 0.5 * (return_batch - values).pow(2).mean()

                self.optimizer.zero_grad()

                policy_loss = value_loss * self.value_loss_coef + action_loss - dist_entropy * self.entropy_coef

                # NOTE: inverse loss will depend on the type of action space
                # print("action_pred_batch shape", action_pred_batch.shape)
                # print("actions_batch shape", actions_batch.shape)
                # print("action_pred_batch type", action_pred_batch.dtype)
                # print("actions_batch type", actions_batch.dtype)
                # print("one hotted actions_batch shape", one_hot(actions_batch, max_val=self.forward_model.action_size).shape)
                one_hot_actions_batch = one_hot(actions_batch, max_val=self.forward_model.action_size).float()
                # print("one hotted actions_batch type", one_hot_actions_batch.dtype)
                # inverse_loss = self.forward_loss_fn(action_pred_batch, actions_batch)
                # inverse_loss = self.forward_loss_fn(action_pred_batch, one_hot_actions_batch)
                # inverse_loss = -(actions_batch * torch.log(action_pred_batch + 1e-15)).sum(1)
                # inverse_loss = -(one_hot_actions_batch * torch.log(action_pred_batch + 1e-15)).sum(1)
                inverse_loss = -(one_hot_actions_batch * torch.log(action_pred_batch + 1e-15)).sum()

                # forward_loss = 0.5 * torch.sum((features_pred_batch - features_batch) ** 2, 1)
                forward_loss = 0.5 * torch.sum((features_pred_batch - features_batch) ** 2, 1).sum()

                # Weighting of forward and inverse loss with the policy loss
                loss = self.lam_pol * policy_loss + \
                       (1 - self.forward_loss_weight) * inverse_loss + \
                       self.forward_loss_weight * forward_loss

                # print("")
                # print("value loss shape", value_loss.shape)
                # print("action loss shape", action_loss.shape)
                # print("forward loss shape", forward_loss.shape)
                # print("inverse loss shape", inverse_loss.shape)
                # print("policy loss shape", policy_loss.shape)
                # print("loss shape", loss.shape)
                # print(policy_loss.item())
                # print(loss.item())

                loss.backward()
                nn.utils.clip_grad_norm_(self.actor_critic.parameters(),
                                         self.max_grad_norm)
                self.optimizer.step()

                value_loss_epoch += value_loss.item()
                action_loss_epoch += action_loss.item()
                dist_entropy_epoch += dist_entropy.item()

        num_updates = self.ppo_epoch * self.num_mini_batch

        value_loss_epoch /= num_updates
        action_loss_epoch /= num_updates
        dist_entropy_epoch /= num_updates

        return value_loss_epoch, action_loss_epoch, dist_entropy_epoch
