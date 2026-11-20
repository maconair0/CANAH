---
name: incident-triage
description: Step-by-step triage when something is reported down, slow or failing, or when the watcher opened an incident ([incident N]). Use for "why is X down", "what is wrong", alarms, outages and incidents.
---

# Triage

1. Establish the symptom from live state, not from the report: which object, since when, what value.
   `incidents` lists what the watcher opened; `canah_status` the controllers in reach.
2. Look one layer down and one layer across: the device under a service, the link under a path, the neighbour
   (`net_read_links`, `net_read_services`; `follow_relations` across domains).
3. Check what changed just before: `network_changes` for the last hours on that controller (the audit log says
   who made a change), and /memories/ for known faults.
4. Report: the symptom, the most likely cause with its evidence, what it affects, what is still unknown.
5. Propose a fix only as a change for approval (see the approvals skill), rehearsed in the digital twin first.
   Never say a fix is applied before it was approved and read back. Note a new recurring fault in /memories/.
