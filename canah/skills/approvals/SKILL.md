---
name: approvals
description: How to propose a change to the system so an operator can approve it. Use before calling any tool that creates, deletes, configures or restarts something.
---

# Proposing a change

1. Read the current state first, with the read tools, so the proposal names real objects.
2. Make the change with the tool that does it, with the real arguments. If that tool belongs to a subagent (a
   controller's tools are its subagent's), delegate the exact change to that subagent: its call is what pauses.
   There is no separate "approvals" step or subagent, and do not ask for permission in text first: the system
   pauses the call and shows it to the operator, and that pause is the request for approval.
3. While it waits, do not call it again or try another way. Several changes that depend on each other are made in
   order, each after the one before it is done.
4. After a decision:
   - approved: report the tool's result; verify it with a read when one exists.
   - edited: the call ran with the operator's arguments; report those.
   - rejected: acknowledge the reason and stop; offer an alternative only if the operator asks.
