"""Validate FCPXML against Apple's DTD.

``conductor/dtd/FCPXMLv1_11.dtd`` is the interchange DTD that ships inside
Final Cut Pro (Interchange.framework). The copy here came from the
CommandPost project's vendored set. Validation needs ``lxml``; without it,
:func:`validate_fcpxml` returns ``None`` so callers can say "not checked"
instead of pretending it passed.
"""

from __future__ import annotations

from pathlib import Path

from .errors import ConductorError

DTD_DIR = Path(__file__).resolve().parent / "dtd"
SUPPORTED = {"1.11": DTD_DIR / "FCPXMLv1_11.dtd"}


def dtd_available() -> bool:
    try:
        import lxml.etree  # noqa: F401
    except ImportError:
        return False
    return True


def validate_fcpxml(path: str | Path) -> list[str] | None:
    """DTD errors for ``path`` (empty when valid), or None when lxml is missing."""
    try:
        from lxml import etree
    except ImportError:
        return None
    file = Path(path)
    try:
        tree = etree.parse(str(file), etree.XMLParser(load_dtd=False, no_network=True))
    except etree.XMLSyntaxError as exc:
        return [f"not well-formed: {exc}"]
    version = tree.getroot().get("version") or ""
    dtd_path = SUPPORTED.get(version)
    if dtd_path is None:
        known = ", ".join(sorted(SUPPORTED))
        raise ConductorError(f"no DTD for FCPXML version {version!r}; this build checks {known}")
    dtd = etree.DTD(str(dtd_path))
    if dtd.validate(tree):
        return []
    return [f"line {error.line}: {error.message}" for error in dtd.error_log.filter_from_errors()]
