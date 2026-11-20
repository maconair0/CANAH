"""
The digital-twin contract, `canah.twin/1`: how CANAH calls any digital twin, and what it expects back.

A twin is anything that can answer "what happens to the network if ...?" with a prediction it can stand
behind. CANAH does not care how it computes (ns-3, GNPy, an analytic model, a lab); it cares that every
twin answers in one shape, so the agent, the write gate, the policy wording and the web page work with any
of them. docs/TWIN_CONTRACT.md is the same text for implementers.

Four calls (in-process: methods of an object; over HTTP: the routes in brackets):

  describe()                        [GET  /twin]            what this twin is and covers         -> Description
  validate(scenario)                [POST /twin/validate]   is this scenario runnable?          -> Validation
  predict(scenario)                 [POST /twin/predict]    the prediction                      -> Result
  check_write(controller, tool,     [POST /twin/check_write] (optional) a controller write,
              arguments, answers)    tried in the twin before it is sent                       -> Check
  extra(name, arguments)            [POST /twin/extra]      (optional) one of the twin's own read-only helpers
                                                            (a snapshot, a slice of a network...), listed in
                                                            describe().extras                   -> any JSON

A scenario is the twin's own JSON (its `describe()` gives an example per domain); it must name its domain
("domain": "<one the twin covers>"). Everything a twin returns is checked against the models below; an
answer that does not fit is reported as the twin's error, never passed to the agent as if it were fine.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

CONTRACT = "canah.twin/1"

# What a twin may decide. The policy behind it is the twin's own; the words are shared.
Action = Literal[
    "propose",              # the change is predicted to keep the requirements: a human may approve it
    "auto_apply_eligible",  # strongly so, and within what may be automated (still approved in CANAH)
    "reject",               # the change is predicted to break a requirement
    "gather_more_data",     # the twin cannot decide (too uncertain, too little trust, a snapshot too old)
    "met",                  # no change asked about: the requirement holds today
    "not_met",              # no change asked about: it does not hold today
]
CheckAction = Literal["propose", "auto_apply_eligible", "reject", "gather_more_data", "ask_operator",
                      "not_modelled", "no_twin", "unknown_tool"]
GO = {"propose", "auto_apply_eligible"}


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class DomainInfo(_Open):
    domain: str                               # the name a scenario's "domain" carries
    title: str = ""
    answers: str = ""                         # what questions this twin answers in this domain
    kpis: List[str] = Field(default_factory=list)
    example: Dict[str, Any] = Field(default_factory=dict)   # a runnable scenario to start from
    fidelity: str = ""
    not_modelled: str = ""


class Extra(_Open):
    name: str
    description: str = ""
    parameters: Dict[str, Any] = Field(default_factory=dict)   # JSON Schema of its arguments


class Description(_Open):
    contract: str = CONTRACT
    name: str
    version: str = ""
    about: str = ""
    domains: List[DomainInfo]
    capabilities: List[Literal["validate", "predict", "check_write", "extras"]] = Field(
        default_factory=lambda: ["validate", "predict"])
    extras: List[Extra] = Field(default_factory=list)
    controller_domains: List[str] = Field(default_factory=list)   # the controllers' domains check_write covers


class Validation(_Open):
    ok: bool
    problems: List[str] = Field(default_factory=list)


class Stats(_Open):
    mean: Optional[float] = None
    p5: Optional[float] = None
    p50: Optional[float] = None
    p95: Optional[float] = None


class Figure(_Open):
    entity: str                               # a demand, a lightpath, a link: whatever the requirement named
    kpi: str
    changed: Optional[Stats] = None           # with the change (or today, when there is none)
    baseline: Optional[Stats] = None          # without it
    effect: Optional[Stats] = None            # changed - baseline, member by member
    calibrated_90: Optional[List[float]] = None   # the range the twin stands behind, from its past errors


class Probability(_Open):
    p: Optional[float] = None                 # that every requirement holds
    interval: Optional[List[float]] = None    # its 95% interval
    members: Optional[int] = None             # how many runs it rests on


class Decision(_Open):
    action: Action
    reasons: List[str] = Field(default_factory=list)


class Trust(_Open):
    tier: Literal["A", "B", "C"]              # A: calibrated on measurements; B: calibrated, less so; C: not
    why: str = ""


class Result(_Open):
    contract: str = CONTRACT
    ok: bool = True
    problems: List[str] = Field(default_factory=list)       # when ok is false: what to fix
    twin: Dict[str, Any] = Field(default_factory=dict)      # {name, version, engine}
    domain: str = ""
    run_id: str = ""
    question: str = ""
    decision: Optional[Decision] = None
    probability: Optional[Probability] = None
    baseline_probability: Optional[Probability] = None
    trust: Optional[Trust] = None
    figures: List[Figure] = Field(default_factory=list)
    requirements: List[Dict[str, Any]] = Field(default_factory=list)
    change_ran: Optional[Dict[str, Any]] = None             # exactly what was simulated
    not_modelled: str = ""
    fidelity: str = ""
    seconds: Optional[float] = None
    cached: bool = False


class Check(_Open):
    check_id: str
    controller: str
    tool: str
    action: CheckAction
    reasons: List[str] = Field(default_factory=list)
    questions: List[str] = Field(default_factory=list)      # ask_operator: what the operator must answer
    next: str = ""                                           # what the agent should do now
    send_as: Optional[str] = None                            # the call to send, when it may go
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result: Optional[Result] = None                          # the prediction behind it, if one ran
    change_ran: Optional[Dict[str, Any]] = None


def checked(model: type, data: Any, who: str) -> Any:
    """`data` as `model`, or a ValueError naming the twin and what is wrong."""
    from pydantic import ValidationError
    try:
        return model.model_validate(data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:6])
        raise ValueError(f"the twin {who!r} answered outside the {CONTRACT} contract ({model.__name__}): {errs}")
