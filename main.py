import copy
import glob
import os
import time
from collections import deque

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

import algo
from arguments import get_args
from envs import make_vec_envs
from model import Policy
from storage import RolloutStorage
from utils import get_vec_normalize
from visualize import visdom_plot
from utils import update_linear_schedule

from tensorboardX import SummaryWriter
import datetime

from rl_algos.curiosity.models import FeatureEncoder, ForwardModel, InverseModel
from rl_algos.utils import one_hot


try:
    import gym_ple
except ImportError:
    print("Could not import Python Learning Environment gym envs")

try:
    import gym_nv_ple
except ImportError:
    print("Could not import non-visual Python Learning Environment gym envs")

try:
    import gridworlds.registration
    from gridworlds.optimal_planner import OptimalPlanner
except ImportError:
    print("Could not import Gridworld environments")

args = get_args()

assert args.algo in ['a2c', 'ppo', 'acktr']
if args.recurrent_policy:
    assert args.algo in ['a2c', 'ppo'], \
        'Recurrent policy is not implemented for ACKTR'

num_updates = int(args.num_env_steps) // args.num_steps // args.num_processes

torch.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)

if args.cuda and torch.cuda.is_available() and args.cuda_deterministic:
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

try:
    os.makedirs(args.log_dir)
except OSError:
    files = glob.glob(os.path.join(args.log_dir, '*.monitor.csv'))
    for f in files:
        os.remove(f)

eval_log_dir = args.log_dir + "_eval"

try:
    os.makedirs(eval_log_dir)
except OSError:
    files = glob.glob(os.path.join(eval_log_dir, '*.monitor.csv'))
    for f in files:
        os.remove(f)


def main():
    torch.set_num_threads(1)
    device = torch.device("cuda:0" if args.cuda else "cpu")

    if args.vis:
        from visdom import Visdom
        viz = Visdom(port=args.port)
        win = None

    ts_str = datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d_%H-%M-%S')
    if args.curiosity:
        log_dir = os.path.join(args.save_dir, 'curiosity', args.algo, args.env_name, 'tensorboard', ts_str)
    else:
        log_dir = os.path.join(args.save_dir, args.algo, args.env_name, 'tensorboard', ts_str)

    tensorboard_writer = SummaryWriter(log_dir=log_dir)

    envs = make_vec_envs(args.env_name, args.seed, args.num_processes,
                        args.gamma, args.log_dir, args.add_timestep, device, False)

    if args.policy == 'default':
        actor_critic = Policy(envs.observation_space.shape, envs.action_space,
            base_kwargs={'recurrent': args.recurrent_policy})
    elif args.policy == 'VIN':
        actor_critic = Policy(envs.observation_space.shape, envs.action_space,
                              base_kwargs={'recurrent': args.recurrent_policy})
    else:
        raise NotImplementedError

    if args.curiosity:
        # TODO: add support for continuous actions
        feature_encoder = FeatureEncoder(state_size=envs.observation_space.shape[0], feature_size=args.feature_size)
        forward_model = ForwardModel(feature_size=args.feature_size, action_size=envs.action_space.n)
        inverse_model = InverseModel(feature_size=args.feature_size, action_size=envs.action_space.n)

    actor_critic.to(device)

    if args.algo == 'a2c':
        agent = algo.A2C_ACKTR(actor_critic, args.value_loss_coef,
                               args.entropy_coef, lr=args.lr,
                               eps=args.eps, alpha=args.alpha,
                               max_grad_norm=args.max_grad_norm)
    elif args.algo == 'ppo':
        if args.curiosity:
            agent = algo.CuriosityPPO(
                forward_model=forward_model, inverse_model=inverse_model, feature_encoder=feature_encoder,
                actor_critic=actor_critic, clip_param=args.clip_param, ppo_epoch=args.ppo_epoch,
                num_mini_batch=args.num_mini_batch, value_loss_coef=args.value_loss_coef,
                entropy_coef=args.entropy_coef, lr=args.lr, eps=args.eps, max_grad_norm=args.max_grad_norm
            )
        else:
            agent = algo.PPO(actor_critic, args.clip_param, args.ppo_epoch, args.num_mini_batch,
                             args.value_loss_coef, args.entropy_coef, lr=args.lr,
                                   eps=args.eps,
                                   max_grad_norm=args.max_grad_norm)
    elif args.algo == 'acktr':
        agent = algo.A2C_ACKTR(actor_critic, args.value_loss_coef,
                               args.entropy_coef, acktr=True)
    else:
        raise NotImplementedError

    rollouts = RolloutStorage(args.num_steps, args.num_processes,
                        envs.observation_space.shape, envs.action_space,
                        actor_critic.recurrent_hidden_state_size)

    obs = envs.reset()
    prev_obs = torch.Tensor(obs.shape)
    rollouts.obs[0].copy_(obs)
    rollouts.to(device)

    episode_rewards = deque(maxlen=10)

    episode_i_rewards = deque(maxlen=10)
    episode_e_rewards = deque(maxlen=10)

    start = time.time()
    for j in range(num_updates):

        if args.use_linear_lr_decay:            
            # decrease learning rate linearly
            if args.algo == "acktr":
                # use optimizer's learning rate since it's hard-coded in kfac.py
                update_linear_schedule(agent.optimizer, j, num_updates, agent.optimizer.lr)
            else:
                update_linear_schedule(agent.optimizer, j, num_updates, args.lr)

        if args.algo == 'ppo' and args.use_linear_lr_decay:      
            agent.clip_param = args.clip_param * (1 - j / float(num_updates))
                
        for step in range(args.num_steps):
            # Sample actions
            with torch.no_grad():
                value, action, action_log_prob, recurrent_hidden_states = actor_critic.act(
                        rollouts.obs[step],
                        rollouts.recurrent_hidden_states[step],
                        rollouts.masks[step])

            # prev_obs = obs.copy()
            prev_obs.copy_(obs)

            # Obser reward and next obs
            obs, reward, done, infos = envs.step(action)

            # print(reward)
            # print(obs)
            # print(done)

            if args.curiosity:
                # TODO: make sure the operations here on on the correct dimensions for the vectors given
                with torch.no_grad():
                    next_features_pred = forward_model(feature_encoder(prev_obs),
                                                       one_hot(action, max_val=forward_model.action_size))

                    # Calculate intrinsic reward
                    # reward_i = args.irsf * torch.sum(torch.square(next_features_pred - feature_encoder(obs)), axis=1, keepdims=False) / 2.
                    reward_i = args.irsf * torch.sum((next_features_pred - feature_encoder(obs)**2), 1, keepdim=True) / 2.

                    # Keep track of intrinsic and extrinsic reward for tensorboard
                    episode_i_rewards.append(reward_i[0])  # NOTE: super dumb hack
                    episode_e_rewards.append(reward[0])  # NOTE: super dumb hack

                    reward = reward * args.erw + reward_i * args.irw

            # print("start of loop")
            for info in infos:
                if 'episode' in info.keys():
                    # print(info['episode']['r'])
                    episode_rewards.append(info['episode']['r'])

            # If done then clean the history of observations.
            masks = torch.FloatTensor([[0.0] if done_ else [1.0]
                                       for done_ in done])

            # NOTE: only curiosity models will use prev_obs
            #       there should be a way to not need it, since the information is already there,
            #       but this is easier for now, ensures no indexing bugs show up
            rollouts.insert(obs, recurrent_hidden_states, action, action_log_prob, value, reward, masks, prev_obs)

        with torch.no_grad():
            next_value = actor_critic.get_value(rollouts.obs[-1],
                                                rollouts.recurrent_hidden_states[-1],
                                                rollouts.masks[-1]).detach()

        rollouts.compute_returns(next_value, args.use_gae, args.gamma, args.tau)

        value_loss, action_loss, dist_entropy = agent.update(rollouts)

        rollouts.after_update()

        # save for every interval-th episode or for the last epoch
        if (j % args.save_interval == 0 or j == num_updates - 1) and args.save_dir != "":
            save_path = os.path.join(args.save_dir, args.algo)
            try:
                os.makedirs(save_path)
            except OSError:
                pass

            # A really ugly way to save a model to CPU
            save_model = actor_critic
            if args.cuda:
                save_model = copy.deepcopy(actor_critic).cpu()

            save_model = [save_model,
                          getattr(get_vec_normalize(envs), 'ob_rms', None)]

            torch.save(save_model, os.path.join(save_path, args.env_name + ".pt"))

        total_num_steps = (j + 1) * args.num_processes * args.num_steps

        # Putting this separate because I want to save the initial weights to see the change
        if j % args.log_interval == 0:
            if args.log_histograms:
                for name, param in actor_critic.named_parameters():
                    tensorboard_writer.add_histogram('parameters/' + name, param.clone().cpu().data.numpy(), total_num_steps)

        if j % args.log_interval == 0 and len(episode_rewards) > 1:
            end = time.time()
            print("Updates {}, num timesteps {}, FPS {} \n Last {} training episodes: mean/median reward {:.1f}/{:.1f}, min/max reward {:.1f}/{:.1f}\n".
                format(j, total_num_steps,
                       int(total_num_steps / (end - start)),
                       len(episode_rewards),
                       np.mean(episode_rewards),
                       np.median(episode_rewards),
                       np.min(episode_rewards),
                       np.max(episode_rewards), dist_entropy,
                       value_loss, action_loss))



            # from this PR: https://github.com/ikostrikov/pytorch-a2c-ppo-acktr/pull/140/files
            tensorboard_writer.add_scalar("mean_reward", np.mean(episode_rewards), total_num_steps)
            tensorboard_writer.add_scalar("median_reward", np.median(episode_rewards), total_num_steps)
            tensorboard_writer.add_scalar("min_reward", np.min(episode_rewards), total_num_steps)
            tensorboard_writer.add_scalar("max_reward", np.max(episode_rewards), total_num_steps)
            tensorboard_writer.add_scalar("dist_entropy", dist_entropy, total_num_steps)
            tensorboard_writer.add_scalar("value_loss", value_loss, total_num_steps)
            tensorboard_writer.add_scalar("action_loss", action_loss, total_num_steps)

            if args.curiosity:
                # print(episode_i_rewards)
                # print(episode_e_rewards)
                # print(episode_rewards)
                tensorboard_writer.add_scalar("mean_intrinsic_reward", np.mean(episode_i_rewards), total_num_steps)
                tensorboard_writer.add_scalar("mean_extrinsic_reward", np.mean(episode_e_rewards), total_num_steps)

        if (args.eval_interval is not None
                and len(episode_rewards) > 1
                and j % args.eval_interval == 0):
            eval_envs = make_vec_envs(
                args.env_name, args.seed + args.num_processes, args.num_processes,
                args.gamma, eval_log_dir, args.add_timestep, device, True)

            vec_norm = get_vec_normalize(eval_envs)
            if vec_norm is not None:
                vec_norm.eval()
                vec_norm.ob_rms = get_vec_normalize(envs).ob_rms

            eval_episode_rewards = []

            obs = eval_envs.reset()
            eval_recurrent_hidden_states = torch.zeros(args.num_processes,
                            actor_critic.recurrent_hidden_state_size, device=device)
            eval_masks = torch.zeros(args.num_processes, 1, device=device)

            while len(eval_episode_rewards) < 10:
                with torch.no_grad():
                    _, action, _, eval_recurrent_hidden_states = actor_critic.act(
                        obs, eval_recurrent_hidden_states, eval_masks, deterministic=True)

                # Obser reward and next obs
                obs, reward, done, infos = eval_envs.step(action)

                eval_masks = torch.FloatTensor([[0.0] if done_ else [1.0]
                                                for done_ in done])
                for info in infos:
                    if 'episode' in info.keys():
                        eval_episode_rewards.append(info['episode']['r'])

            eval_envs.close()

            print(" Evaluation using {} episodes: mean reward {:.5f}\n".
                format(len(eval_episode_rewards),
                       np.mean(eval_episode_rewards)))

        if args.vis and j % args.vis_interval == 0:
            try:
                # Sometimes monitor doesn't properly flush the outputs
                win = visdom_plot(viz, win, args.log_dir, args.env_name,
                                  args.algo, args.num_env_steps)
            except IOError:
                pass


if __name__ == "__main__":
    main()
