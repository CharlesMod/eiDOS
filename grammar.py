"""GBNF grammar for the tick output contract (BIBLE §2.1: hard channel separation).

Constrained decoding makes the contract structural: the model literally cannot emit a
malformed tool call, an unknown tool name, or invalid args JSON — the sampler refuses
the tokens. This deletes the parse-error tick tax at the source (v1 burned a whole
tick coaching the model after every sloppy call) and makes fail_kind=no_such_tool
unrepresentable, because valid tool names are enumerated in the grammar itself.

Shape (ordering is load-bearing):
    root ::= thought? reply? toolcall?   (at least one part; reply BEFORE toolcall)
The reply-before-tool order + the require_reply flag exist for phase 3: when Boss is
waiting, the tick's grammar can REQUIRE a reply and it streams first, so per-sentence
TTS starts before the tool call is even generated — reply-first enforced by the
sampler, not begged for in the prompt.

The grammar governs FORM only. CONTENT guidance (which tool fits, arg semantics)
stays in the prompt; semantic arg validation is the phase-6 validate gate's job.

Verified live on this llama.cpp build (HouseAI-Llama, 2026-06-10): GBNF accepted on
/v1/chat/completions, {m,n} repetition bounds supported, and the :8088 monitor tap
forwards the "grammar" field untouched.
"""

from __future__ import annotations

# JSON value grammar — the llama.cpp json.gbnf shape, with bounded whitespace so the
# sampler can't pad forever between tokens.
_JSON_RULES = r"""
jobject ::= "{" jws ( jstring ":" jws jvalue ( "," jws jstring ":" jws jvalue )* )? "}" jws
jvalue ::= jobject | jarray | jstring | jnumber | ( "true" | "false" | "null" ) jws
jarray ::= "[" jws ( jvalue ( "," jws jvalue )* )? "]" jws
jstring ::= "\"" ( [^"\\\x7F\x00-\x1F] | "\\" ( ["\\bfnrt/] | "u" jhex jhex jhex jhex ) )* "\"" jws
jhex ::= [0-9a-fA-F]
jnumber ::= "-"? ( "0" | [1-9] [0-9]* ) ( "." [0-9]+ )? ( [eE] [-+]? [0-9]+ )? jws
jws ::= [ \t\n]{0,12}
"""


def _name_alternatives(tool_names) -> str:
    names = sorted(set(tool_names))
    if not names:
        raise ValueError("tool registry is empty - cannot build a grammar")
    for n in names:
        if not n.replace("_", "").isalnum():
            raise ValueError(f"tool name not grammar-safe: {n!r}")
    return " | ".join(f'"{n}"' for n in names)


def build_tick_grammar(tool_names, *, require_reply: bool = False) -> str:
    """The tick-output grammar for the given live tool registry.

    require_reply (phase 3, Boss-waiting ticks): the reply block becomes mandatory
    and still precedes any tool call, so reply tokens stream first.
    """
    if require_reply:
        root = "root ::= thought? reply ws toolcall? ws"
    else:
        # At least one of thought / reply / toolcall — an empty output is illegal.
        root = "root ::= ( thought reply? ws toolcall? | reply ws toolcall? | toolcall ) ws"
    return "\n".join([
        root,
        # Free-form thought: any run of text that never opens a tag ('<' would start
        # a structured block; the model phrases around it).
        "thought ::= [^<] [^<]*",
        # Reply text may contain "<x" but never "</" — the closing tag terminates it.
        'reply ::= "<reply>" rtext "</reply>"',
        'rtext ::= ( [^<] | "<" [^/<] )*',
        'toolcall ::= "<tool>" toolname "</tool>" ws "<args>" jws jobject "</args>"',
        f"toolname ::= {_name_alternatives(tool_names)}",
        'ws ::= [ \\t\\n]{0,12}',
        _JSON_RULES.strip(),
    ])


_cache_key = None
_cache_value = None


def tick_grammar_cached(tool_names, *, require_reply: bool = False) -> str:
    """build_tick_grammar memoized on registry contents — a hot-loaded skill enters
    the next tick's grammar; an unchanged registry costs nothing."""
    global _cache_key, _cache_value
    key = (tuple(sorted(set(tool_names))), require_reply)
    if key != _cache_key:
        _cache_value = build_tick_grammar(tool_names, require_reply=require_reply)
        _cache_key = key
    return _cache_value
