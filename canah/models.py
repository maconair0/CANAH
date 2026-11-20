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
# keep_alive 24h: an idle model is not unloaded after half an hour, so the first question after a pause does
# not wait for 18 GB to load again. Ollama still unloads it when another model needs the GPU memory.
from langchain_ollama import ChatOllama  # noqa: E402

model = ChatOllama(model="qwen3:30b-a3b-instruct-2507-q4_K_M", temperature=0, num_ctx=32768, num_predict=4096,
                   keep_alive="24h", reasoning=False, client_kwargs={"timeout": 300})
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
        return init_chat_model(spec, temperature=0, num_ctx=32768, num_predict=4096, keep_alive="24h",
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


# ----- the model chosen on the web page (Model page; root) -----
# CANAH_HOME/model.json   {"provider", "model", "base_url", "num_ctx"}: the page's choice for every role
# CANAH_HOME/keys.json    provider -> API key, readable by CANAH's user only; put in the environment at start
PROVIDERS = {
    "ollama":         {"title": "Ollama (local)", "package": "langchain_ollama", "key_env": "", "pip": "langchain-ollama"},
    "anthropic":      {"title": "Anthropic (Claude)", "package": "langchain_anthropic", "key_env": "ANTHROPIC_API_KEY", "pip": "langchain-anthropic",
                       "examples": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]},
    "openai":         {"title": "OpenAI", "package": "langchain_openai", "key_env": "OPENAI_API_KEY", "pip": "langchain-openai",
                       "examples": ["gpt-5.5", "gpt-5.5-mini"]},
    "openai_compatible": {"title": "OpenAI-compatible server (vLLM, LM Studio, ...)", "package": "langchain_openai",
                       "key_env": "OPENAI_COMPATIBLE_API_KEY", "pip": "langchain-openai", "base_url": True},
    "google_genai":   {"title": "Google Gemini", "package": "langchain_google_genai", "key_env": "GOOGLE_API_KEY", "pip": "langchain-google-genai",
                       "examples": ["gemini-2.5-pro", "gemini-2.5-flash"]},
    "groq":           {"title": "Groq", "package": "langchain_groq", "key_env": "GROQ_API_KEY", "pip": "langchain-groq"},
}
_page = {"choice": None, "home": None}


def installed(provider):
    import importlib.util
    spec = PROVIDERS.get(provider) or {}
    return bool(spec) and importlib.util.find_spec(spec["package"]) is not None


def _read(path):
    import json
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _write(path, data):
    import json
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_page_choice(home):
    """Read the page's choice and keys (at every agent build): keys go into the environment, the choice is
    used by get() unless CANAH_MODEL is set."""
    home = Path(home)
    for prov, key in _read(home / "keys.json").items():
        env = (PROVIDERS.get(prov) or {}).get("key_env")
        if env and key:
            os.environ[env] = key
    choice = _read(home / "model.json") or None
    if choice != _page["choice"]:
        _built.clear()
    _page.update(choice=choice, home=home)
    return choice


def build_choice(choice):
    """A chat model from the page's choice."""
    prov, name = choice.get("provider"), str(choice.get("model") or "").strip()
    if prov not in PROVIDERS:
        raise ValueError(f"unknown provider {prov!r}: one of {', '.join(PROVIDERS)}")
    if not name:
        raise ValueError("say which model")
    if not installed(prov):
        raise ValueError(f"{PROVIDERS[prov]['title']} is not installed here: .venv/bin/pip install {PROVIDERS[prov]['pip']}")
    if prov == "ollama":
        from langchain_ollama import ChatOllama
        kw = {"base_url": choice["base_url"]} if choice.get("base_url") else {}
        return ChatOllama(model=name, temperature=0, num_ctx=int(choice.get("num_ctx") or 32768), num_predict=4096,
                          keep_alive="24h", reasoning=False, client_kwargs={"timeout": 300}, **kw)
    if prov == "openai_compatible":
        if not choice.get("base_url"):
            raise ValueError("an OpenAI-compatible server needs its base URL (http://host:port/v1)")
        return init_chat_model(f"openai:{name}", base_url=choice["base_url"], temperature=0, max_tokens=4096, timeout=120,
                               max_retries=2, use_responses_api=False,
                               api_key=os.getenv("OPENAI_COMPATIBLE_API_KEY") or "not-needed")
    return make(f"{prov}:{name}")


def save_page_choice(home, choice=None, key=None, provider=None, clear=False):
    """Keep the page's choice (None with clear=True: back to this file's model) and/or a provider's key."""
    home = Path(home)
    if key is not None and provider:
        keys = _read(home / "keys.json")
        if key:
            keys[provider] = key
        else:
            keys.pop(provider, None)
            env = (PROVIDERS.get(provider) or {}).get("key_env")
            if env:
                os.environ.pop(env, None)
        _write(home / "keys.json", keys)
    if clear:
        try:
            (home / "model.json").unlink()
        except FileNotFoundError:
            pass
    elif choice is not None:
        _write(home / "model.json", {k: v for k, v in choice.items() if k in ("provider", "model", "base_url", "num_ctx") and v})
    return load_page_choice(home)


def keys_set(home):
    return {p: bool(k) for p, k in _read(Path(home) / "keys.json").items()}


def get(role="model"):
    """The model for a role: the environment override if set, else the page's choice, else this file's line."""
    if role not in _ROLES:
        raise ValueError(f"a model role is one of {_ROLES}, not {role!r}")
    if role not in _built:
        spec = os.getenv(_ENV[role]) or (os.getenv("CANAH_MODEL") if role != "model" else "")
        if spec:
            _built[role] = make(spec)
        elif _page["choice"]:
            _built[role] = _built.get("model") if role != "model" and "model" in _built else build_choice(_page["choice"])
        else:
            _built[role] = globals()[role]
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
