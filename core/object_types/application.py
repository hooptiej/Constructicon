"""Standalone Windows application support (issue #446): an .exe that runs
as-is rather than installing something.

An .exe is one of two things, and the file can't always say which:
- A clear installer-framework fingerprint (NSIS, Inno Setup, InstallShield,
  WiX Burn, embedded MSI; see _pe.installer_framework) is claimed by the
  installer type's higher-priority sniffer, with no question asked.
- Every other .exe lands here, and pre_store_fn stores it as an application
  AND queues "installer or standalone app?" in the admin's Needs your input
  (answering retypes the row). When the name hints at an installer ("Setup"
  in the filename or version info), or most of the file is data appended
  after the program (or that data opens with a 7z/zip/cab/NSIS/Inno marker,
  #587), "installer" is the suggested answer and the question says why;
  otherwise "application" is.

Either way the object page offers a Reclassify action (this file's for
app -> installer, installer.py's for the reverse), since nothing else can
change a row's type by hand.

Facts come from the PE headers and version resource via _pe.py, read once
at upload (embedded_metadata_fn, the STL #449 pattern); the build timestamp,
when plausible, also seeds content_date. Nothing is executed.
"""

import datetime
import logging

from .. import besteffort, datefmt, storage
from . import _pe, _preview, register, ObjectTypeSpec, PreStore, ThumbnailSource, TypeAction

STATS_KEY = "application_stats"
log = logging.getLogger("constructicon.application")


def sniff(path, filename):
    """sniff_fn: a real PE executable (MZ + PE header)."""
    return _pe.is_pe(path)


def _payload_reason(path):
    """A sentence saying why the file's shape suggests an installer, or None. Best-effort: an
    unreadable file just gives no hint."""
    try:
        info = _pe.overlay_info(path)
    except Exception as e:
        besteffort.warn(log, "application: couldn't read the overlay", e, path=str(path))
        return None
    if not _pe.looks_like_self_extractor(info):
        return None
    mb = info["overlay_bytes"] / (1024 * 1024)
    size = f"{mb:.1f} MB" if mb >= 1 else f"{info['overlay_bytes'] // 1024} KB"
    if info.get("overlay_signature"):
        return (f"Most installers carry their payload this way: {size} of this file is data appended after the "
                f"program, and it starts like a {info['overlay_signature']} archive.")
    return (f"Most of this file is an embedded payload: {size} ({info['overlay_ratio']:.0%}) is data appended "
            f"after the program, the usual shape of a self-extracting installer.")


def pre_store(candidate):
    """pre_store_fn: always ask "installer or standalone app?"; the name
    hint only decides which answer is preselected."""
    if candidate.path is None:
        return PreStore.accept()
    name = candidate.filename or "this .exe"
    hint = _pe.name_hint(candidate.path, candidate.filename)
    question = f"Is {name} an installer or a standalone app?"
    if hint:
        question += " (Its name or version info mentions setup/install, but no installer framework was found.)"
    # #587 item 3: the strongest installer signal is the shape of the file: most of it is data
    # appended after the program (an embedded payload), or that data opens with an archive signature.
    payload = _payload_reason(candidate.path)
    if payload:
        question += f" ({payload})"
    suggest_installer = bool(hint or payload)
    return PreStore.needs_decision(
        kind="exe_classification",
        question=question,
        options=[
            {"key": "installer", "label": "Installer (sets software up)", "suggested": suggest_installer},
            {"key": "application", "label": "Standalone app (runs as-is)", "suggested": not suggest_installer},
        ],
        provisional_type="application",
    )


def get_embedded_metadata(path):
    """embedded_metadata_fn: PE facts once at upload; build time -> content_date."""
    try:
        info = _pe.facts(path)
    except Exception as e:
        print(f"Application PE parse failed for {path}: {e!r}")
        return {}
    out = {"type_metadata": {STATS_KEY: info}}
    if info.get("built"):
        out["content_date"] = info["built"]
    return out


def get_properties(row):
    """properties_fn: format the stored facts (re-parsing only for a row
    that predates the hook). {} on any failure."""
    try:
        stats = (row.get("type_metadata") or {}).get(STATS_KEY)
        if not stats and row.get("stored_filename"):
            path = storage.path_for(row["stored_filename"])
            if path.exists():
                stats = (get_embedded_metadata(path).get("type_metadata") or {}).get(STATS_KEY)
        if not stats:
            return {}
        kind = ".NET application" if stats.get("dotnet") else "Windows application"
        props = {"Format": f"{kind} ({stats.get('subsystem', 'unknown')}, {stats.get('architecture', '?')})"}
        for key, label in (("name", "Name"), ("version", "Version"), ("publisher", "Publisher"),
                           ("original_filename", "Original filename"), ("description", "Description"),
                           ("copyright", "Copyright")):
            if stats.get(key):
                props[label] = str(stats[key])
        if stats.get("signer") and not stats.get("publisher"):
            props["Signed by"] = str(stats["signer"])  # #587 item 4: no CompanyName, so the signing certificate
        if stats.get("overlay_bytes", 0) >= 64 * 1024:
            props["Appended data"] = f"{stats['overlay_bytes'] / (1024 * 1024):.1f} MB"  # #587 item 3
        if "signed" in stats:
            # Windows' own binaries are catalog-signed, so "no embedded
            # signature" is the honest wording, not "unsigned".
            props["Signed"] = "Yes (Authenticode)" if stats["signed"] else "No embedded signature"
        if stats.get("built"):
            props["Built"] = datefmt.iso_day(stats["built"])
        return props
    except Exception as e:
        print(f"Application properties failed for {row.get('slug')}: {e!r}")
        return {}


def preview(ctx):
    """preview_fn: nothing to render; file icon (live) or download link (export)."""
    return _preview.file_icon(ctx)


register(ObjectTypeSpec(
    key="application",
    label="Application",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    caption_capable=False,
    extensions=frozenset({".exe"}),
    sniff_fn=sniff,  # priority 0: installer.py's fingerprint sniffer runs first
    pre_store_fn=pre_store,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,  # PE facts once, at upload
    preview_fn=preview,
    actions=(TypeAction(
        "reclassify-installer", "Reclassify as installer",
        lambda row: _pe.reclassify(row, "installer"),
        confirm="File this .exe as an installer instead of a standalone app?",
        applies_fn=_pe.is_exe_row,
    ),),
    badge_icon="\U0001F5A5",  # desktop computer
    badge_text="APP",
))
