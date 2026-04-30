"""Role prompt boundary.

Roles define the instructions, prompt context, and role-specific guidance used
to ask an agent for work. They should not invoke agents, persist task reports,
or decide lifecycle transitions directly; those responsibilities live in
``litehive.agents``, ``litehive.tasks``, and ``litehive.lifecycle``.
"""
