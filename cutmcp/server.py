"""MCP surface. Thin on purpose — all logic lives in a tier module.

If a tool function here grows past about fifteen lines, logic has leaked out
of `extract`, `decide` or `assemble` and should be pushed back.

The docstrings below are the tool descriptions the calling agent reads, so
they are written for that reader rather than for us.
"""

from __future__ import annotations

from typing import Any

# MCP SDK 2.x. In v1 this was `from mcp.server.fastmcp import FastMCP`;
# that name will fail here.
from mcp.server.mcpserver import MCPServer

from . import assemble, decide, extract

mcp = MCPServer(
    name="cutmcp",
    instructions=(
        "Turns raw interview footage into a cut timeline. Call ingest once per "
        "file, then cut with a brief and a duration target, then review the "
        "least-confident cuts before exporting an EDL."
    ),
)


@mcp.tool()
async def ingest(path: str, force: bool = False) -> dict[str, Any]:
    """Extract a transcript, pauses, speakers and loudness from a media file.

    Deterministic, cached and free — no model is involved and no cost is
    incurred. Needs a transcript: either a whisper JSON sidecar beside the
    media (`foo.mp4` → `foo.json`) or whisperx/whisper on PATH. Diarization
    matters; speaker labels are what make interview logic work.

    Returns the `media_id` every other tool takes. Pass `force=True` to
    re-extract after editing the transcript sidecar.
    """
    m = extract.ingest(path, force=force)
    fillers = sum(1 for s in m.segments if s.is_trivial_filler)
    return {
        "media_id": m.media_id,
        "duration": round(m.duration, 2),
        "segments": len(m.segments),
        "trivial_filler_segments": fillers,
        "speakers": m.speakers,
    }


@mcp.tool()
async def estimate(media_id: str, brief: str) -> dict[str, Any]:
    """Price a `cut` before running it, without spending anything.

    Reports how many requests and questions the brief will produce and what
    the input tokens will cost. Roughly three cents per hour of footage;
    output tokens are free.
    """
    return decide.cost_estimate(extract.load(media_id), brief)


@mcp.tool()
async def cut(
    media_id: str, brief: str, target_seconds: float, cut_penalty: float = 0.6
) -> dict[str, Any]:
    """Cut the footage down to `target_seconds` of the material serving `brief`.

    The brief is the editorial instruction — "a 3-minute explainer on why the
    migration failed" — and every line is judged against it.

    `cut_penalty` is the tightness/smoothness tradeoff, and it is the main
    knob worth turning. It prices every cut in the timeline against how
    clean that cut would feel. Low (0.2) buys density: more, shorter clips,
    tighter but choppier. High (1.5) buys smoothness: fewer, longer clips
    that breathe, at the cost of carrying weaker lines along. The default of
    0.6 sits between them. Total duration barely moves; what changes is how
    many cuts you are asking the viewer to absorb.

    The result never overruns the target. Review before exporting.
    """
    media = extract.load(media_id)
    decs = await decide.decide(media, brief)
    tl = assemble.build(media, decs, target_seconds, brief, cut_penalty=cut_penalty)
    return {
        "timeline_id": tl.timeline_id,
        "clips": len(tl.clips),
        "duration": round(tl.duration, 2),
        "target": target_seconds,
        "cut_penalty": cut_penalty,
        "cuts_to_review": len(tl.cuts),
    }


@mcp.tool()
async def review(timeline_id: str, limit: int = 20) -> dict[str, Any]:
    """The cuts most worth a human's attention, least confident first.

    This is the real deliverable of a cut: not the whole timeline, but the
    handful of joins the model was least sure about. Each entry carries where
    the cut falls, how clean it is expected to feel, and the line it lands
    after, so it can be judged without opening an NLE.
    """
    tl = assemble.load_timeline(timeline_id)
    cuts = assemble.review_queue(tl, limit)
    return {
        "timeline_id": tl.timeline_id,
        "returned": len(cuts),
        "of": len(tl.cuts),
        "cuts": [c.__dict__ for c in cuts],
    }


@mcp.tool()
async def export_timeline(
    timeline_id: str, out_dir: str = "./out", fps: int = 24
) -> dict[str, Any]:
    """Write the timeline to disk as JSON and a CMX3600 EDL.

    Also writes `.otio` when opentimelineio is installed. `fps` sets the
    timecode base of the EDL and must match the project's frame rate.
    Returns the path written for each format.
    """
    return assemble.export(assemble.load_timeline(timeline_id), out_dir, fps)


@mcp.resource("timeline://{timeline_id}")
async def timeline_resource(timeline_id: str) -> str:
    """The full timeline as JSON: every clip, every cut, every confidence."""
    return assemble.load_timeline(timeline_id).to_json()


def main() -> None:
    """Run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
