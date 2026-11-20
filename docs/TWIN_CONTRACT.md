# The digital-twin contract: `canah.twin/1`

CANAH asks a digital twin what a change or a failure will do to a network before the network sees it.
Any twin can answer, provided it speaks this contract. That includes [netwins](https://github.com/maconair0/NetTwin), a lab's own simulator, an analytic model, or a vendor's planning tool.
CANAH relies on the contract and nothing else: the agent's tools, the write gate, the wording of decisions and the web page are the same whichever twin is in use.

The contract is enforced in code. [`canah/twins/contract.py`](../canah/twins/contract.py) holds a Pydantic model for every shape below. An answer that does not fit is reported as that twin's error, naming the field. It is never handed to the agent as if it were fine.

## Two ways to plug a twin in

| Kind | What CANAH needs | Registered as |
|---|---|---|
| **HTTP service** | a base URL serving the routes below; optionally a bearer token | `{"name": "lab-twin", "kind": "http", "url": "http://10.0.0.9:8420", "token_env": "LAB_TWIN_TOKEN"}` |
| **Python object** | an importable `module:factory` whose object has the methods below | `{"name": "netwins", "kind": "python", "spec": "netwins.contract:twin", "path": "~/netwins"}` |

Register a twin in either of two places:
- under `twins:` in `canah.yaml`;
- on the web page (Digital twins → Add a custom twin; needs admin).

The page also chooses which twin is in use (admin). The choice takes effect at the next question, with no restart.

`token_env` is the **name** of an environment variable on CANAH's host. CANAH reads the token from it at each call. The token itself is never written to CANAH's files.

## The calls

| Call | HTTP | Python | Required |
|---|---|---|---|
| describe | `GET /twin` | `describe() -> dict` | yes |
| validate | `POST /twin/validate` `{"scenario": {...}}` | `validate(scenario) -> dict` | yes |
| predict | `POST /twin/predict` `{"scenario": {...}}` | `predict(scenario) -> dict` | yes |
| check_write | `POST /twin/check_write` `{"controller", "tool", "arguments", "answers"}` | `check_write(controller, tool, arguments, answers) -> dict` | no: list `"check_write"` in `capabilities` |
| extra | `POST /twin/extra` `{"name", "arguments"}` | `extra(name, arguments) -> any JSON` | no: list `"extras"` in `capabilities` |

A **scenario** is the twin's own JSON: CANAH does not prescribe it. It must carry `"domain": "<one the twin describes>"`, and the twin's description gives a runnable example for each domain. The agent copies that example, puts the network and the what-if in, validates it, then predicts.

### describe → Description

```json
{
  "contract": "canah.twin/1",
  "name": "netwins",
  "version": "0.4",
  "about": "one line: what this twin is",
  "domains": [
    {
      "domain": "optical",
      "title": "Optical (WDM)",
      "answers": "whether lightpaths keep their margin after a fibre cut, ...",
      "kpis": ["gsnr_db", "margin_db", "pre_fec_ber", "throughput_mbps", "loss_ratio"],
      "example": {"domain": "optical", "...": "a runnable scenario"},
      "fidelity": "GNPy GN model per lightpath; fluid packet layer",
      "not_modelled": "transients, wavelength assignment, ..."
    }
  ],
  "capabilities": ["validate", "predict", "check_write", "extras"],
  "controller_domains": ["packet"],
  "extras": [{"name": "subnetwork", "description": "...", "parameters": {"type": "object", "...": "JSON Schema"}}]
}
```

- `answers`, `kpis`, `fidelity` and `not_modelled` are what the agent reads to choose the twin's domain, and to say honestly what the answer does not cover.
- `controller_domains` lists the controllers' domains whose writes `check_write` can judge. CANAH's agentic MCP names a controller's domain, for example `packet`.

### validate → Validation

```json
{"ok": false, "problems": ["fibre F-ab: length_km 9000 is outside the validated range 1..6000"]}
```

A problem is a sentence the agent can act on: it fixes the scenario and validates again.

### predict → Result

```json
{
  "contract": "canah.twin/1",
  "ok": true,
  "twin": {"name": "netwins", "version": "0.4", "engine": "gnpy 3.0.1"},
  "domain": "optical",
  "run_id": "4f1c2a9b07de",
  "decision": {"action": "reject", "reasons": ["p=0.000 (95% interval 0.000-0.083) is below 0.9"]},
  "probability": {"p": 0.0, "interval": [0.0, 0.083], "members": 40},
  "baseline_probability": {"p": 1.0, "interval": [0.912, 1.0], "members": 40},
  "trust": {"tier": "A", "why": "calibrated on measurements"},
  "figures": [
    {"entity": "LP-1", "kpi": "margin_db",
     "changed":  {"mean": -0.16, "p5": -0.55, "p50": -0.16, "p95": 0.21},
     "baseline": {"mean": 4.47, "p5": 4.47, "p50": 4.47, "p95": 4.47},
     "effect":   {"mean": -4.63, "p5": -5.0, "p50": -4.6, "p95": -4.3},
     "calibrated_90": [-1.25, 8.67]}
  ],
  "requirements": [{"demand": "LP-1", "kpi": "margin_db", "op": ">=", "value": 3}],
  "change_ran": {"events": [{"type": "link_down", "target": "F-ab"}]},
  "not_modelled": "static QoT: no transients ...",
  "fidelity": "GNPy GN model per lightpath",
  "seconds": 3.8,
  "cached": false
}
```

When the scenario cannot run: `{"contract": "canah.twin/1", "ok": false, "problems": ["..."]}`.

**`decision.action`** is one of:

| Action | Meaning | What CANAH does |
|---|---|---|
| `propose` | the change is predicted to keep every requirement | the agent may propose it; a human approves |
| `auto_apply_eligible` | strongly so, and within what may be automated | the same: CANAH still asks a human |
| `reject` | the change is predicted to break a requirement | the agent says why, with the figures, and does not send it |
| `gather_more_data` | the twin cannot decide: too uncertain, too little trust, a stale snapshot | the agent asks the operator how to proceed |
| `met` / `not_met` | no change was asked about: does the requirement hold today? | reported as a statement about today |

The decision is the twin's own policy. CANAH reports it and never overrides it on its own.

**`probability`**: `p` is the share of the twin's runs (or its own estimate) in which every requirement held with the change. `interval` is a 95% interval for `p`, and `members` how many runs it rests on. A deterministic twin gives `p` of 0 or 1 with `members: 1`.

**`trust.tier`**:
- **A:** calibrated against measurements, and it holds.
- **B:** calibrated, but less (fewer points, or synthetic truth only).
- **C:** not calibrated. A twin at C should reject, but never let a change go ahead.

**`figures`**: one per requirement (entity and KPI), or per entity and KPI when there is no requirement.
- `changed` is with the change; `baseline` and `effect` are without it, and the difference run by run.
- `calibrated_90` is the range the twin stands behind, from its past errors, when it has them.

**`change_ran`** is exactly what was simulated. The agent is told to report only this, so it cannot describe a change it did not run.

### check_write → Check (optional)

CANAH's write gate calls this when the agent is about to send a write to a controller, and the twin lists `check_write`. The twin translates the write into a change on its picture of the network, predicts, and decides:

```json
{
  "check_id": "8e0b1c2d3a",
  "controller": "onos",
  "tool": "add_path",
  "action": "ask_operator",
  "reasons": ["add_path(...) gives no rate: how much traffic will it carry, in Mbit/s? (answers.rate_mbps)"],
  "questions": ["... in Mbit/s? (answers.rate_mbps)"],
  "next": "Ask the operator these questions, then check again with their answers in `answers`.",
  "send_as": "onos__add_path with exactly these arguments: {...}",
  "arguments": {"name": "vid1", "from": "pe-a", "to": "pe-b"},
  "result": null,
  "change_ran": null
}
```

`action` takes one of these values:

| Action | Meaning |
|---|---|
| `propose`, `auto_apply_eligible` | the write may go: CANAH's approval still applies |
| `reject` | it may not |
| `gather_more_data` | the twin cannot settle it |
| `ask_operator` | the write leaves out something the twin needs; `questions` says what |
| `not_modelled` | the twin cannot represent this kind of change |
| `no_twin` | this controller's domain is not the twin's: the write goes through approval alone |
| `unknown_tool` | not one of the controller's tools |

`result` is the prediction behind the check (a Result), when one ran.

What the gate does with a check:
- A write runs if its check is `propose`, `auto_apply_eligible` or `no_twin`, or if an operator overrode a check of the same call. "The same call" includes the same change written differently, judged by `change_ran`.
- Otherwise the gate returns the check to the agent instead of sending the write.
- An override is a tool the agent calls on the operator's word (`twin_override`, with the operator's reason). That override itself waits for approval.

### extra (optional)

A twin's own read-only helpers, listed in `describe().extras` with a JSON Schema of their arguments. The agent calls them through one tool, `twin_extra(name, arguments)`. netwins has four:
- `snapshots`: the networks netwins has a snapshot of;
- `inspect`: one network's latest snapshot, summarised;
- `subnetwork`: the slice of a network to build a scenario from, with every demand that crosses it;
- `mirror`: read a controller's network now into a fresh snapshot.

## What the agent sees

The tools stay the same whichever twin is in use:
- `twin_list`: the twin in use, its domains and extras;
- `twin_schema(domain)`: a domain's example;
- `twin_validate`, `twin_predict`;
- `twin_check_write`, `twin_override`;
- `twin_extra`.

`twin_predict` returns the Result, rounded and with `change_ran` marked "say only this". The full Result is kept in `CANAH_HOME/twin_runs/<run_id>.json`.

## A twin in fifty lines

[`examples/custom_twin.py`](../examples/custom_twin.py) is a complete twin over HTTP, using only the standard library. Its model is a fluid one: capacities, and demands on shortest paths sharing links. Replace its `run()` with your model and keep the rest:

```bash
python examples/custom_twin.py --port 8430
# CANAH: Digital twins → Add a custom twin → http://127.0.0.1:8430 → Use this twin
```

`tests_canah/test_twins.py` checks it against CANAH, along with a twin that answers outside the contract.
