from collections import defaultdict

import numpy as np
from stable_baselines.common.vec_env import VecEnvWrapper

from modelfree.scheduling import LinearAnnealer
from modelfree.utils import DummyModel

REW_TYPES = set(('sparse', 'dense'))


def _anneal(reward_dict, reward_annealer):
    sparse_weight, dense_weight = 1, 1
    if reward_annealer is not None:
        c = reward_annealer()
        assert 0 <= c <= 1
        sparse_weight = 1 - c
        dense_weight = c
    return (reward_dict['sparse'] * sparse_weight
            + reward_dict['dense'] * dense_weight)


class RewardShapingVecWrapper(VecEnvWrapper):
    """
    A more direct interface for shaping the reward of the attacking agent.
    - shaping_params schema: {'sparse': {k: v}, 'dense': {k: v}, **kwargs}
    """
    def __init__(self, venv, agent_idx, logger, shaping_params, reward_annealer=None):
        super().__init__(venv)
        assert shaping_params.keys() == REW_TYPES
        self.shaping_params = {}
        for rew_type, params in shaping_params.items():
            for rew_term, weight in params.items():
                self.shaping_params[rew_term] = (rew_type, weight)

        self.reward_annealer = reward_annealer
        self.agent_idx = agent_idx
        self.logger = logger

        self.ep_rew_dict = defaultdict(list)
        self.step_rew_dict = defaultdict(lambda: [[] for _ in range(self.num_envs)])

    def log_callback(self):
        """Logs various metrics. This is given as a callback to PPO2.learn()"""
        num_episodes = len(self.ep_rew_dict['sparse'])
        if num_episodes == 0:
            return

        means = {}
        for rew_type, rews in self.ep_rew_dict.items():
            assert len(rews) == num_episodes
            means[rew_type] = sum(rews) / num_episodes
            self.logger.logkv(f'ep{rew_type}mean', means[rew_type])

        overall_mean = _anneal(means, self.reward_annealer)
        self.logger.logkv('eprewmean_true', overall_mean)

        for rew_type in self.ep_rew_dict:
            self.ep_rew_dict[rew_type] = []

    def reset(self):
        return self.venv.reset()

    def step_wait(self):
        obs, rew, done, infos = self.venv.step_wait()
        for env_num in range(self.num_envs):
            # Compute shaped_reward for each rew_type
            shaped_reward = {k: 0 for k in REW_TYPES}
            for rew_term, rew_value in infos[env_num][self.agent_idx].items():
                if rew_term not in self.shaping_params:
                    continue
                rew_type, weight = self.shaping_params[rew_term]
                shaped_reward[rew_type] += weight * rew_value

            # Compute total shaped reward, optionally annealing
            rew[env_num] = _anneal(shaped_reward, self.reward_annealer)

            # Log the results of an episode into buffers and then pass on the shaped reward
            for rew_type, val in shaped_reward.items():
                self.step_rew_dict[rew_type][env_num].append(val)

            if done[env_num]:
                for rew_type in REW_TYPES:
                    self.ep_rew_dict[rew_type].append(sum(self.step_rew_dict[rew_type][env_num]))
                    self.step_rew_dict[rew_type][env_num] = []

        return obs, rew, done, infos


class NoisyAgentWrapper(DummyModel):
    def __init__(self, agent, noise_annealer, noise_type='gaussian'):
        """
        Wrap an agent and add noise to its actions
        :param agent: (BaseRLModel) the agent to wrap
        :param noise_annealer: Annealer.get_value - presumably the noise should be decreased
        over time in order to get the adversarial policy to perform well on a normal victim.
        :param noise_type: str - the type of noise parametrized by noise_annealer's value.
        Current options are [gaussian]
        """
        super().__init__(policy=agent, sess=agent.sess)
        self.noise_annealer = noise_annealer
        self.noise_generator = self._get_noise_generator(noise_type)

    @staticmethod
    def _get_noise_generator(noise_type):
        noise_generators = {
            'gaussian': lambda x, size: np.random.normal(scale=x, size=size)
        }
        return noise_generators[noise_type]

    def predict(self, observation, state=None, mask=None, deterministic=False):
        original_actions, states = self.policy.predict(observation, state, mask, deterministic)
        action_shape = original_actions.shape
        noise_param = self.noise_annealer()

        noise = self.noise_generator(noise_param, action_shape)
        noisy_actions = original_actions * (1 + noise)
        return noisy_actions, states


def apply_env_wrapper(single_env, shaping_params, agent_idx, logger, scheduler):
    anneal_frac = shaping_params.get('anneal_frac')
    if anneal_frac is not None:
        annealer = LinearAnnealer(1, 0, anneal_frac).get_value
        scheduler.set_func('rew_shape', annealer)
        rew_shape_func = scheduler.get_func('rew_shape')
    else:
        # In this case, the different reward types are weighted differently
        # but reward is not annealed over time.
        rew_shape_func = None

    return RewardShapingVecWrapper(single_env, agent_idx=agent_idx, logger=logger,
                                   shaping_params=shaping_params['weights'],
                                   reward_annealer=rew_shape_func)


def apply_victim_wrapper(victim, noise_params, scheduler):
    victim_noise_anneal_frac = noise_params.get('victim_noise_anneal_frac', 0)
    victim_noise_param = noise_params.get('victim_noise_param', 0)
    assert victim_noise_anneal_frac > 0, \
        "victim_noise_anneal_frac must be greater than 0 if using a NoisyAgentWrapper."

    noise_anneal_func = LinearAnnealer(victim_noise_param, 0,
                                       victim_noise_anneal_frac).get_value
    scheduler.set_func('noise', noise_anneal_func)
    return NoisyAgentWrapper(victim, noise_annealer=scheduler.get_func('noise'))
