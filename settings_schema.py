"""Curated settings schema for the dashboard Settings menu.

A friend should be able to point eiDOS at their own model and tune the useful knobs WITHOUT hand-editing
config.toml. This module is the single source of truth for which config values the UI exposes and how
each maps onto config.toml's shape (section + key) so `config.save_overrides` writes a clean overlay.

Each field: id, section (None = a top-level key), key (the config.toml key), attr (the Config dataclass
attribute, for reading current values), type, label, and optional help/picker/min/max/secret. Keeping
the map here (not in dashboard.py) makes it unit-testable and keeps the HTTP layer thin.
"""
from __future__ import annotations

# group -> list of field specs. Order is display order.
SPEC = [
    ("Model & inference", [
        ("llm", "url", "llm_url", "str", "Endpoint URL (OpenAI-compatible)",
         "e.g. http://127.0.0.1:11434/v1 (Ollama) · :1234/v1 (LM Studio) · :8080 (llama.cpp)"),
        ("llm", "model", "llm_model", "str", "Model", "name the server expects", {"picker": True}),
        ("llm", "temperature", "llm_temperature", "float", "Temperature", ""),
        ("llm", "max_tokens", "llm_max_tokens", "int", "Max tokens / reply", ""),
        ("llm", "top_p", "llm_top_p", "float", "top_p", ""),
        ("llm", "top_k", "llm_top_k", "int", "top_k", ""),
        ("llm", "min_p", "llm_min_p", "float", "min_p", ""),
        ("llm", "presence_penalty", "llm_presence_penalty", "float", "Presence penalty", ""),
        ("llm", "frequency_penalty", "llm_frequency_penalty", "float", "Frequency penalty", ""),
        ("llm", "repeat_penalty", "llm_repeat_penalty", "float", "Repeat penalty", ""),
        ("llm", "request_timeout_s", "llm_request_timeout_s", "int", "Request timeout (s)", ""),
        ("llm", "grammar_enabled", "llm_grammar_enabled", "bool", "Grammar-constrained decoding",
         "forces valid tool-call syntax; turn off if your server rejects GBNF"),
        ("context", "max_total_chars", "context_max_total_chars", "int", "Context budget (chars)",
         "~4 chars/token; keep under your model's window"),
    ]),
    ("Behavior & identity", [
        (None, "creature_mode", "creature_mode", "bool", "Creature mode",
         "an open-ended creature with no preset house mission"),
        ("persona", "enabled", "persona_enabled", "bool", "Persona", ""),
        ("nervous", "goaltension_enabled", "nervous_goaltension_enabled", "bool", "Goal-tension drive",
         "an unfinished objective keeps it awake & acting (initiative when idle)"),
        ("nervous", "temperament_enabled", "nervous_temperament_enabled", "bool", "Temperament drift",
         "slow initiative/persistence/caution shaped by its own track record"),
    ]),
    ("Tempo & resources", [
        ("tick", "interval_s", "tick_interval_s", "float", "Idle tick interval (s)", ""),
        ("tick", "interval_active_s", "tick_interval_active_s", "float", "Active tick interval (s)", ""),
        ("nervous", "metabolism_enabled", "nervous_metabolism_enabled", "bool", "Metabolism (energy economy)", ""),
        ("safety", "cmd_timeout_s", "cmd_timeout_s", "float", "Command soft-timeout (s)", ""),
    ]),
    ("Memory & learning", [
        ("knowledge", "embedding_enabled", "knowledge_embedding_enabled", "bool", "Semantic memory (embeddings)",
         "needs the embedding model — run setup_embedding.py / install with --with-embeddings"),
        ("knowledge", "recall_top_k", "knowledge_recall_top_k", "int", "Recall results / tick", ""),
        ("nervous", "learning_enabled", "nervous_learning_enabled", "bool", "Reward learning + dreams", ""),
    ]),
    ("Optional features (off by default)", [
        ("nervous", "enabled", "nervous_enabled", "bool", "Nervous system", ""),
        ("self_improvement", "self_edit_enabled", "self_edit_enabled", "bool", "Self-editing", ""),
        ("delegate", "enabled", "delegate_enabled", "bool", "Delegate to a coding agent (needs pi)", ""),
        ("ide", "enabled", "ide_enabled", "bool", "Browser IDE", ""),
        ("nervous", "power_enabled", "power_enabled", "bool", "Battery/solar power sensing (Renogy BLE)", ""),
    ]),
    ("Access & ports", [
        ("dashboard", "port", "dashboard_port", "int", "Dashboard port", "restart required"),
        ("dashboard", "token", "dashboard_token", "str", "Access token (blank = open on trusted LAN)",
         "", {"secret": True}),
    ]),
]

# Fast lookup: field id -> spec tuple.
_BY_ID = {}
for _group, _fields in SPEC:
    for _f in _fields:
        _BY_ID[_f[1] if _f[0] is None else f"{_f[0]}.{_f[1]}"] = _f

# Sampler fields that are stored PER MODEL (config [llm.profiles.<model>]) rather than globally: their
# displayed value tracks the active model, and saving routes them under the selected model's profile.
PER_MODEL_FIELDS = {f"llm.{_k}" for _k in
                    ("temperature", "top_p", "top_k", "min_p",
                     "presence_penalty", "frequency_penalty", "repeat_penalty")}


def _field_id(section, key):
    return key if section is None else f"{section}.{key}"


def _coerce(typ, raw):
    """Coerce an incoming JSON value to the field's type, raising ValueError on a bad value."""
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if typ == "int":
        return int(raw)
    if typ == "float":
        return float(raw)
    return str(raw)


def current_settings(config) -> list:
    """Grouped current values for the UI:
    [{group, fields:[{id,key,label,type,help,value,picker,secret,per_model}]}].
    Per-model sampler fields show the ACTIVE model's effective value (base ⊕ its profile)."""
    from config import active_sampler
    sampler = active_sampler(config)      # active model's effective sampler values
    out = []
    for group, fields in SPEC:
        items = []
        for spec in fields:
            section, key, attr, typ, label, help_ = spec[:6]
            extra = spec[6] if len(spec) > 6 else {}
            fid = _field_id(section, key)
            per_model = fid in PER_MODEL_FIELDS
            val = sampler[key] if (per_model and key in sampler) else getattr(config, attr, None)
            if extra.get("secret") and val:
                val = "********"          # never echo a configured token back to the browser
            items.append({"id": fid, "key": key, "label": label, "type": typ,
                          "help": help_, "value": val, "picker": bool(extra.get("picker")),
                          "secret": bool(extra.get("secret")), "per_model": per_model})
        out.append({"group": group, "fields": items})
    return out


def model_profiles(config) -> dict:
    """{model: {field_id: value}} of per-model sampler values, so the UI can repopulate the sampler
    fields the instant the model dropdown changes. Covers every model with a stored profile plus the
    active model (so switching away and back always shows real numbers)."""
    from config import active_sampler
    models = set(getattr(config, "llm_profiles", None) or {}) | {config.llm_model}
    out = {}
    for m in models:
        s = active_sampler(config, m)
        out[m] = {f"llm.{k}": s[k] for k in s}
    return out


def build_overrides(payload: dict):
    """Turn a {field_id: value} payload from the UI into config.toml-shaped overrides for save_overrides.
    Returns (overrides, errors). Unknown ids are ignored; type errors are collected, not raised. A
    masked secret ('********') is skipped so an unchanged token isn't overwritten with the mask."""
    overrides, errors = {}, []
    # Per-model sampler fields save under this model's profile ([llm.profiles.<model>]).
    target_model = (payload or {}).get("llm.model")
    for fid, raw in (payload or {}).items():
        spec = _BY_ID.get(fid)
        if spec is None:
            continue
        section, key, attr, typ = spec[0], spec[1], spec[2], spec[3]
        extra = spec[6] if len(spec) > 6 else {}
        if extra.get("secret") and raw == "********":
            continue
        try:
            val = _coerce(typ, raw)
        except (ValueError, TypeError):
            errors.append(f"{fid}: expected {typ}")
            continue
        if fid in PER_MODEL_FIELDS and target_model:
            # llm -> profiles -> <model> -> key
            overrides.setdefault("llm", {}).setdefault("profiles", {}).setdefault(target_model, {})[key] = val
        elif section is None:
            overrides[key] = val
        else:
            overrides.setdefault(section, {})[key] = val
    return overrides, errors
