from __future__ import annotations

import os
from pathlib import Path

import pytest
from doc2mcp.curate.agent import CurationOptions, curate
from doc2mcp.curate.manifest import Manifest, name_error
from doc2mcp.model import ApiSpec
from pydantic import BaseModel, ConfigDict

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY is not set",
    ),
]

_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = _ROOT / "tests" / "corpus" / "openapi"
_BUDGET_TOKENS = 200_000
_BUDGET_USD = 5.0
_SPECS = {
    "petstore": _CORPUS / "petstore-3.1.json.endpoints.json",
    "stripe": _CORPUS / "stripe-api.json.endpoints.json",
    "github": _CORPUS / "github-api.json.endpoints.json",
}
_REFERENCES: dict[str, tuple[int, tuple[str, ...]]] = {
    "github": (80, ("list_issues", "create_issue")),
    "petstore": (3, ("create_pet", "get_pet")),
    "stripe": (40, ("list_customers", "create_customer", "create_refund")),
}


class _Rubric(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_score: int
    heuristic_score: int
    note: str


def _score(
    manifest: Manifest,
    spec: ApiSpec,
    target: int,
    required: tuple[str, ...],
) -> dict[str, float]:
    names = [tool.name for tool in manifest.tools]
    valid = sum(name_error(name) is None for name in names)
    covered = {step.endpoint_id for tool in manifest.tools for step in tool.steps}
    covered.update(item.endpoint_id for item in manifest.excluded)
    present = sum(name in names for name in required)
    distance = abs(len(names) - target)
    return {
        "closeness": 1 - min(1.0, distance / target),
        "coverage": len(covered & {endpoint.id for endpoint in spec.endpoints})
        / len(spec.endpoints),
        "names": valid / len(names) if names else 0,
        "required": present / len(required) if required else 1,
    }


def _judge(agent: Manifest, heuristic: Manifest) -> _Rubric:
    from anthropic import Anthropic

    prompt = (
        "Score each tool list from 1 to 5 for a coding agent. "
        "5 means the tools match user intents, hide plumbing, and stay within a small set. "
        "Return only the scores.\n\n"
        f"Agent tools: {', '.join(tool.name for tool in agent.tools)}\n"
        f"Heuristic tools: {', '.join(tool.name for tool in heuristic.tools)}\n"
    )
    with Anthropic().beta.messages.stream(
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
        model="claude-sonnet-5-5",
        output_config={"effort": "high"},
        output_format=_Rubric,
        thinking={"type": "adaptive"},
    ) as stream:
        final = stream.get_final_message()
    parsed = final.parsed_output
    if not isinstance(parsed, _Rubric):
        raise AssertionError(final.stop_reason)
    return parsed


@pytest.mark.parametrize("name", ["petstore", "stripe", "github"])
def test_agent_beats_heuristic(name: str, tmp_path: Path) -> None:
    spec_path = _SPECS[name]
    (tmp_path / "endpoints.json").write_bytes(spec_path.read_bytes())
    spec = ApiSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
    options = CurationOptions(
        budget_tokens=_BUDGET_TOKENS,
        budget_usd=_BUDGET_USD,
        max_tools=20,
        recurate=True,
    )
    agent = curate(tmp_path, options)
    heuristic = curate(tmp_path, CurationOptions(mode="heuristic", offline=True, max_tools=20))
    assert agent.manifest is not None
    assert heuristic.manifest is not None
    target, required = _REFERENCES[name]
    agent_score = _score(agent.manifest, spec, target, required)
    heuristic_score = _score(heuristic.manifest, spec, target, required)
    rubric = _judge(agent.manifest, heuristic.manifest)
    spent = agent.manifest.curation.usage.estimated_usd
    assert agent_score["coverage"] >= heuristic_score["coverage"]
    assert agent_score["closeness"] >= heuristic_score["closeness"]
    assert agent_score["names"] >= heuristic_score["names"]
    assert agent_score["required"] >= heuristic_score["required"]
    assert rubric.agent_score > rubric.heuristic_score
    assert 1 <= rubric.agent_score <= 5
    assert spent <= _BUDGET_USD
