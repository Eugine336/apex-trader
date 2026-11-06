# RL subsystem retirement

The reinforcement-learning package is removed from Apex. It was a dormant,
checkpoint-dependent side path and is not part of the native Brain's decision
authority.

Removed:
- the `rl/` package, PPO environments/trainers, shadow bridge and authority stages;
- RL-only training scripts, checkpoint initialization and RL data validation;
- RL-only tests, checkpoint path helpers, dashboard status, watchdog state and
  runtime context wiring;
- the PyTorch dependency and its dedicated CI installation.

Retained:
- the native Brain and its competing-hypothesis reasoning;
- statistical/adaptive learning from realised trade outcomes;
- calibration, Bayesian reasoning, historical replay and evidence provenance;
- the shared data-junction and repository-root isolation contracts.

This retirement does not replace one policy learner with another. Apex's native
Brain remains the only decision authority. No broker credentials, live orders,
or broker execution paths are changed by this retirement.
