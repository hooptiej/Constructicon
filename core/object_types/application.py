"""Standalone Windows application support (issue #446): an .exe that runs
as-is rather than installing something.

An .exe is one of two things, and the file can't always say which:
- A clear installer-framework fingerprint (NSIS, Inno Setup, InstallShield,
  WiX Burn, embedded MSI; see _pe.installer_framework) is claimed by the
  installer type's higher-priority sniffer, with no question asked.
- Every other .exe lands here, and pre_store_fn stores it as an application
  AND queues "installer or standalone app?" in the admin's Needs your input
  (answering retypes the row). When only the name hints at an installer
  ("Setup" in the filename or version info), "installer" is the suggested
  answer; otherwise "application" is.

Either way the object page offers a Reclassify action (this file's for
app -> installer, installer.py's for the reverse), since nothing else can
change a row's type by hand.

Facts come from the PE headers and version resource via _pe.py, read once
at upload (embedded_metadata_fn, the STL #449 pattern); the build timestamp,
when plausible, also seeds content_date. Nothing is executed.
"""

import datetime

from .. import storage
from . import _pe, _preview, register, ObjectTypeSpec, PreStore, ThumbnailSource, TypeAction

STATS_KEY = "application_stats"


def sniff(path, filename):
    """sniff_fn: a real PE executable (MZ + PE header)."""
    return _pe.is_pe(path)


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
    return PreStore.needs_decision(
        kind="exe_classification",
        question=question,
        options=[
            {"key": "installer", "label": "Installer (sets software up)", "suggested": hint},
            {"key": "application", "label": "Standalone app (runs as-is)", "suggested": not hint},
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
        if "signed" in stats:
            # Windows' own binaries are catalog-signed, so "no embedded
            # signature" is the honest wording, not "unsigned".
            props["Signed"] = "Yes (Authenticode)" if stats["signed"] else "No embedded signature"
        if stats.get("built"):
            props["Built"] = datetime.datetime.fromtimestamp(
                stats["built"], datetime.timezone.utc).strftime("%Y-%m-%d")
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
