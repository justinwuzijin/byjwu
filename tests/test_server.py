"""The MCP surface: five tools, one resource, and no logic of its own."""

from __future__ import annotations

import inspect
import json

from conftest import BRIEF

from cutmcp import server

TOOLS = {"ingest", "estimate", "cut", "review", "export_timeline"}


async def test_registers_exactly_five_tools_and_one_resource_template():
    """Gate 11."""
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == TOOLS
    templates = await server.mcp.list_resource_templates()
    assert [t.uri_template for t in templates] == ["timeline://{timeline_id}"]


async def test_every_tool_describes_itself_to_the_calling_agent():
    for tool in await server.mcp.list_tools():
        assert tool.description and len(tool.description) > 80, tool.name
    cut = next(t for t in await server.mcp.list_tools() if t.name == "cut")
    assert "cut_penalty" in cut.description
    assert "tightness" in cut.description and "smoothness" in cut.description


def test_tools_stay_thin():
    """Past ~15 lines, logic has leaked out of a tier module."""
    for name in TOOLS | {"timeline_resource"}:
        fn = getattr(server, name)
        src = inspect.getsource(fn)
        body = src.split('"""')[-1].strip().splitlines()
        code = [l for l in body if l.strip() and not l.strip().startswith("#")]
        assert len(code) <= 15, f"{name} is {len(code)} lines"


async def test_ingest_estimate_cut_review_export(media_path, tmp_path):
    ing = await server.ingest(str(media_path))
    assert ing["segments"] == 600
    assert ing["speakers"] == ["GUEST", "HOST"]
    assert ing["trivial_filler_segments"] > 0

    est = await server.estimate(ing["media_id"], BRIEF)
    assert est["requests"] > 0 and est["approx_usd"] > 0

    cut = await server.cut(ing["media_id"], BRIEF, 180.0)
    assert cut["duration"] <= 180.0 and cut["clips"] > 1

    rev = await server.review(cut["timeline_id"], limit=20)
    assert rev["returned"] == min(20, cut["cuts_to_review"])
    confs = [c["confidence"] for c in rev["cuts"]]
    assert confs == sorted(confs)

    out = await server.export_timeline(cut["timeline_id"], str(tmp_path))
    assert set(out) >= {"json", "edl"}

    doc = json.loads(await server.timeline_resource(cut["timeline_id"]))
    assert doc["timeline_id"] == cut["timeline_id"]
    assert len(doc["clips"]) == cut["clips"]


async def test_cut_penalty_reaches_the_dp_through_the_tool(media_path):
    mid = (await server.ingest(str(media_path)))["media_id"]
    loose = await server.cut(mid, BRIEF, 180.0, cut_penalty=1.5)
    tight = await server.cut(mid, BRIEF, 180.0, cut_penalty=0.2)
    assert tight["clips"] > loose["clips"]
    assert tight["timeline_id"] != loose["timeline_id"]
