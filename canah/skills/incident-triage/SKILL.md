---
name: incident-triage
description: Step-by-step triage when something is reported down, slow or failing. Use for "why is X down", "X is not working", alarms and incidents.
---

# Triage

1. Establish the symptom from live state, not from the report: which object, since when, what value.
2. Look one layer down and one layer across: the device under a service, the link under a path, the neighbour.
3. Check what changed recently (a change log or history tool, if there is one) and /memories/ for known faults.
4. Report: the symptom, the most likely cause with its evidence, what is still unknown.
5. Propose a fix only as a change for approval (see the approvals skill). Note a new recurring fault in /memories/.
