"""GENOME_V3 personality matrix: 3 wired axes (affiliation, boldness, playfulness) + a named-type
matrix, surfaced as an operator-only "nature". These pin the HARD invariants the adversarial design
pass demanded — most importantly the "personality is pressure, never a script" firewall (the nature
label / new axes must never reach a creature-facing prompt) and "no day-one badge".
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import genome as G          # noqa: E402
import itertools            # noqa: E402


class TestPresetsAndTypes:
    def test_every_preset_component_within_the_cap(self):
        # invariant-b ceiling: a 70/30 blend of two <=1.0 vectors stays <=1.0, so no birth baseline can
        # ride past the [0.38,0.62] clamp. A future preset edit must not break this.
        for name, vec in G.PRESETS.items():
            assert len(vec) == len(G.ALL_LATENTS), name
            for c in vec:
                assert -1.0 <= c <= 1.0, f"{name}: component {c} exceeds the +/-1.0 cap"

    def test_ten_primary_types_and_a_nature_adjective_each(self):
        assert len(G.PRIMARY_NAMES) == 10
        for n in G.PRIMARY_NAMES:
            assert n in G._NATURE_ADJ and n in G._NATURE_NOUN

    def test_draw_produces_seven_axes_and_a_type(self):
        g = G.draw_germline(2026)
        assert set(g["latents"]) == set(G.ALL_LATENTS)
        assert g["type"]["primary"] in G.PRESETS
        assert g["type"]["secondary"] in G.PRESETS
        assert g["type"]["primary"] != g["type"]["secondary"]


class TestInvariantB_NoDayOneBadge:
    def test_baseline_stays_inside_disposition_bands_under_all_extremes(self):
        # even with every latent railed at +/-LATENT_BOUND, express_baselines must land within
        # [0.38,0.62] (BASELINE clamp), which sits strictly inside disposition()'s 0.34/0.66.
        for combo in itertools.product([-G.LATENT_BOUND, G.LATENT_BOUND], repeat=len(G.ALL_LATENTS)):
            b = G.express_baselines(dict(zip(G.ALL_LATENTS, combo)))
            for ax, v in b.items():
                assert G.BASELINE_LO <= v <= G.BASELINE_HI, f"{ax}={v} escaped the band"
                assert 0.34 < v < 0.66, f"{ax}={v} could pre-label a newborn by disposition"


class TestFailOpenMigration:
    def test_absent_v3_axes_read_as_neutral(self):
        # an old genome lacking the new axes: latents default 0.0 -> genes 1.0 (neutral), no baseline tilt
        old = {"sensitivity": 0.5, "openness": -0.3, "tenacity": 0.2, "tempo": 0.1}  # 4 axes only
        genes = G.express_genes(old)
        assert genes["operator_pull"] == 1.0
        assert genes["levity"] == 1.0
        assert genes["press_scale"] == 1.0
        # playfulness absent -> its term contributes 0 to restlessness (openness/tenacity still apply)
        base = G.express_genes({"openness": -0.3, "tenacity": 0.2})["restlessness"]
        assert genes["restlessness"] == base


class TestNatureNaming:
    def test_nature_name_reads_type_ids(self):
        assert G.nature_name({"type": {"primary": "Builder", "secondary": "Explorer"}}) == "Curious Builder"

    def test_nature_name_fails_open_to_a_neutral_label(self):
        assert isinstance(G.nature_name({}), str) and G.nature_name({}).strip()

    def test_nature_name_falls_back_to_nearest_type_for_pre_v3_genomes(self):
        # a v1/v2 upgrade has no stored type ids -> derive from latents, never crash
        lat = {"openness": 1.4, "boldness": 1.0}  # explorer-ish
        name = G.nature_name({"latents": lat})
        assert isinstance(name, str) and " " in name


class TestNeverAScript:
    """The load-bearing invariant-a firewall: the nature label and the new axis names must NEVER
    appear in any creature-facing renderer. This is the DURABLE form (a permanent regression gate),
    not a one-time ship check — a future edit that injects ident.nature into context fails HERE."""

    FORBIDDEN = ("nature", "affiliation", "boldness", "playfulness",
                 "operator_pull", "levity", "press_scale", "PRESETS", "primary", "secondary")

    def test_new_axes_and_nature_never_reach_creature_facing_renderers(self):
        root = Path(__file__).resolve().parent.parent
        for fname in ("context.py", "prompts.py", "persona.py"):
            src = (root / fname).read_text(encoding="utf-8")
            # strip comments/docstrings-ish: we only care about live references; a bare word check is
            # the same dynamic-firewall style the repo uses for xp/levels/bets/coins.
            for word in self.FORBIDDEN:
                assert not re.search(r"\b" + re.escape(word) + r"\b", src), \
                    f"{fname} references '{word}' — personality must stay pressure, never a script"
