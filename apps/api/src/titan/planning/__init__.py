"""Autonomous planning mechanics (Vol 6 §4, TITAN-WFL-015/016/017/018/019): the
auto sprint planner, sprint ceremonies, estimation feedback, milestone tracking,
and the blocker-detection recovery ladder.

Per TITAN-BCK-004 only ``service`` crosses the module boundary; other domains
import ``titan.planning.service``. The pure sub-modules (``planner``, ``milestones``,
``estimation``, ``blockers``) are the side-effect-free core the service composes.
"""
