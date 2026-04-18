"""Scheduled tasks subsystem — recurring agent invocations driven by cron schedules.

Agents can register tasks (e.g. Lisa's daily inbox review, Maya's weekly social
draft) that fire on a cron schedule. Each invocation spawns the agent's CLI
with the task's prompt and routes the output to a Slack destination.
"""
