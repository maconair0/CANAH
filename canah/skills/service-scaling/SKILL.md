---
name: service-scaling
description: "Change a live service's capacity or route: more or less bandwidth, a faster optical mode, a move to another path. Use when asked to scale, upgrade, downgrade, resize or move an existing service."
---

# Scaling a live service

1. Read it: `net_read_service` on its controller; note its rate, its route and what rides on it (`follow_relations`).
2. Rehearse the new value in the twin: the scenario as it is, with the change in `change.set` (a rate,
   `rate_gbps` on a lightpath) or as a new path; requirements for the service and for what shares its links.
3. If the twin supports it, make the change with the contract's `net_adjust` (or `net_reroute` for a move) on
   that controller. A controller without `adjust`: find its own tool with `network_tools`, or propose a
   make-before-break (provision the new one, then tear down the old) as two approved steps.
4. Read the service back afterwards and report the before and after.
