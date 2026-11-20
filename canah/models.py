"""Model Initialization File

Configures the LLM the agent runs on.

Default: local Ollama, qwen3:30b-a3b-instruct (no API key, nothing leaves this machine).

═══════════════════════════════════════════════════════════════════════════
  ⚠  IMPORTANT: install the matching package BEFORE swapping providers
═══════════════════════════════════════════════════════════════════════════

  Provider              Install command                        Already installed?
  --------------------  -------------------------------------  ------------------
  Ollama (default)      -                                      yes
  Anthropic             -                                      yes
  OpenAI / compatible   -                                      yes
  Google Gemini         .venv/bin/pip install langchain-google-genai   no - install first
  AWS Bedrock           .venv/bin/pip install langchain-aws            no - install first
  Groq                  .venv/bin/pip install langchain-groq           no - install first

═══════════════════════════════════════════════════════════════════════════

To swap providers:
  1. Run the install command above (if needed).
  2. Comment out the ACTIVE model lines below.
  3. Uncomment the section for the provider you want.
  4. Set the provider's key in CANAH/.env (see the notes inline).

Three models:
  model          the main agent: plans, calls tools, talks to the operator
  strong_model   steps that need stronger reasoning (a subagent asks for it with `model: strong_model`)
  fast_model     small jobs: summarising, titling, classifying

The model must support tool calling. Keep a cap on the reply length (max_tokens /
num_predict): without one, a model that loops keeps writing until its context is full.

Environment override: CANAH_MODEL (and CANAH_STRONG_MODEL, CANAH_FAST_MODEL), as
"provider:model", replaces the active lines without editing this file (the test bed
uses this to compare models).
"""

import os  # noqa: F401  # used in commented-out model examples below
from pathlib import Path

from dotenv import load_dotenv

# CANAH/.env; a variable already set in the shell wins.
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=False)

from langchain.chat_models import init_chat_model  # noqa: E402

# ═══ ACTIVE: local Ollama ════════════════════════════════════════════════════
# No key, no quota. Ollama must be running with the model pulled:
#   ollama pull qwen3:30b-a3b-instruct-2507-q4_K_M      (~18 GB)
# Another host: base_url="http://host:11434"
from langchain_ollama import ChatOllama  # noqa: E402

model = ChatOllama(model="qwen3:30b-a3b-instruct-2507-q4_K_M", temperature=0, num_ctx=32768, num_predict=4096,
                   keep_alive="30m", reasoning=False, client_kwargs={"timeout": 300})
strong_model = model
fast_model = model

# ─── Fallback (optional) ──────────────────────────────────────────────────────
# If the main model fails (down, rate-limited, erroring), the agent switches to this one.
#
# fallback_model = init_chat_model("anthropic:claude-haiku-5-5", temperature=0, max_tokens=4096, timeout=60)
fallback_model = None

# ═══ Anthropic ═══════════════════════════════════════════════════════════════
# Requires ANTHROPIC_API_KEY in .env
#
# model = init_chat_model("anthropic:claude-sonnet-5-5", temperature=0, max_tokens=4096, timeout=120, max_retries=2)
# strong_model = init_chat_model("anthropic:claude-opus-5-5", temperature=0, max_tokens=4096, timeout=180, max_retries=2)
# fast_model = init_chat_model("anthropic:claude-haiku-5-5", temperature=0, max_tokens=2048, timeout=60, max_retries=2)

# ═══ OpenAI ══════════════════════════════════════════════════════════════════
# Requires OPENAI_API_KEY in .env
#
# model = init_chat_model("openai:gpt-5.5", temperature=0, max_tokens=4096, timeout=120, max_retries=2,
#                         use_responses_api=False)
# strong_model = model
# fast_model = model

# ═══ Other local Ollama models ═══════════════════════════════════════════════
# Pull first, e.g.:  ollama pull qwen2.5:7b
# Note: small models (7-8B) mis-call tools and guess names more often; keep strong_model on a larger one.
#
# model = ChatOllama(model="qwen2.5:7b", temperature=0, num_ctx=16384, num_predict=4096, client_kwargs={"timeout": 300})
# model = ChatOllama(model="qwen3-vl:30b-a3b-instruct-q4_K_M", temperature=0, num_ctx=32768, num_predict=4096,
#                    client_kwargs={"timeout": 300})        # the same family, reads images too

# ═══ OpenAI-compatible servers (vLLM, LM Studio, a gateway) ══════════════════
# No extra install (langchain-openai is installed).
#
# from langchain_openai import ChatOpenAI
# model = ChatOpenAI(model="<model name>", base_url="http://host:8000/v1", api_key=os.getenv("OPENAI_API_KEY", "none"),
#                    temperature=0, max_tokens=4096, timeout=120, max_retries=2)
# strong_model = model
# fast_model = model

# OpenRouter: hosted models through the same API. Requires OPENROUTER_API_KEY in .env
#
# from langchain_openai import ChatOpenAI
# model = ChatOpenAI(model="<provider/model>", base_url="https://openrouter.ai/api/v1",
#                    api_key=os.environ["OPENROUTER_API_KEY"], temperature=0, max_tokens=4096, timeout=120, max_retries=2)
# strong_model = model
# fast_model = model

# ═══ Cloud providers (install first, see the table above) ════════════════════
# ─── Google Gemini ────────────────────────────────────────────────────────────
# Requires GOOGLE_API_KEY in .env
#
# model = init_chat_model("google_genai:gemini-3.8-flash", temperature=0, max_tokens=4096, timeout=120, max_retries=2)

# ─── AWS Bedrock ──────────────────────────────────────────────────────────────
# Requires AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY in .env; set the region below
#
# from langchain_aws import ChatBedrockConverse
# model = ChatBedrockConverse(model_id="anthropic.claude-sonnet-4-6", region_name="us-east-1", temperature=0,
#                             max_tokens=4096)

# ─── Groq ─────────────────────────────────────────────────────────────────────
# Requires GROQ_API_KEY in .env
#
# model = init_chat_model("groq:llama-3.3-70b-versatile", temperature=0, max_tokens=4096, timeout=120, max_retries=2)


# ═════════════════════════════════════════════════════════════════════════════
# Below: how the rest of CANAH reads these. No need to edit.
# ═════════════════════════════════════════════════════════════════════════════

def make(spec):
    """A chat model from a "provider:model" string, capped and with a timeout (a subagent's
    `model: provider:model`, and the CANAH_MODEL override)."""
    spec = str(spec).strip()
    if ":" not in spec:
        raise ValueError(f"model {spec!r}: write it as provider:model, e.g. anthropic:claude-sonnet-5-5")
    provider = spec.split(":", 1)[0]
    if provider == "ollama":
        return init_chat_model(spec, temperature=0, num_ctx=32768, num_predict=4096, keep_alive="30m",
                               reasoning=False, client_kwargs={"timeout": 300})
    if provider == "openai":
        return init_chat_model(spec, temperature=0, max_tokens=4096, timeout=120, max_retries=2,
                               use_responses_api=False)
    if provider in ("anthropic", "groq"):
        return init_chat_model(spec, temperature=0, max_tokens=4096, timeout=120, max_retries=2)
    return init_chat_model(spec, temperature=0, timeout=120, max_retries=2)


_ROLES = ("model", "strong_model", "fast_model")
_ENV = {"model": "CANAH_MODEL", "strong_model": "CANAH_STRONG_MODEL", "fast_model": "CANAH_FAST_MODEL"}
_built = {}


def get(role="model"):
    """The model for a role: the environment override if set, else this file's line."""
    if role not in _ROLES:
        raise ValueError(f"a model role is one of {_ROLES}, not {role!r}")
    if role not in _built:
        spec = os.getenv(_ENV[role]) or (os.getenv("CANAH_MODEL") if role != "model" else "")
        _built[role] = make(spec) if spec else globals()[role]
    return _built[role]


def _name(m):
    for attr in ("model", "model_name", "model_id"):
        value = getattr(m, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(m).__name__


def describe():
    """Which model each role runs on, as "provider:model" where it can be told."""
    out = {}
    for role in _ROLES:
        m = get(role)
        provider = type(m).__name__.replace("Chat", "").lower()
        out[role] = f"{provider}:{_name(m)}"
    return out
