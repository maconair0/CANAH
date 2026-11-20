---
name: intent-to-change
description: "Turn an operator's intent (\"10 Gbit/s from A to B\", \"connect site X to the data centre\", \"a backup path for service S\") into a checked change: find the ends, compute a path, rehearse it in the digital twin, then propose the write for approval. Use whenever the operator states an outcome rather than a command."
---

# From an intent to a change

1. **Pin the intent down.** The two ends (which controller holds each, as the operator names them), the rate,
   any constraint (latency, diversity from another service, a domain to avoid). Ask only for what you cannot
   read: look the ends up with `net_read_devices` (no controller: every one).
2. **Find a way.** `net_compute_path` on the controller that holds both ends. Across domains,
   `follow_relations` shows where they meet; plan one segment per controller.
3. **Rehearse it.** Build the twin's scenario from what you read (`twin_schema`), add the new demand or
   lightpath as the change, state the requirement (the rate, the latency), `twin_validate`, then `twin_predict`.
   Report the decision, probability and trust tier as given.
4. **Propose it** only if the twin's decision lets it: `net_provision` with the controller and its arguments
   (each segment its own call). The call pauses for an operator's approval: that pause is the confirmation.
   Do not ask "shall I?" in text.
5. After approval, read it back (`net_read_service`) and say what you found.
