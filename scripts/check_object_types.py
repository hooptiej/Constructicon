#!/usr/bin/env python3
"""
Object types contract self-check (#448).

Run inside the app container before deploying:
  docker exec <container> python3 scripts/check_object_types.py

Verifies that all registered types comply with the contract v2:
  - preview_fn and properties_fn are present (enforced at registration)
  - Synthetic items render without exception in both live and export modes
  - Unescaped data doesn't appear in previews
  - properties_fn returns a dict of str→str

Exit 1 on any failure.
"""

import sys
import os
import markupsafe
from pathlib import Path

# Add repo root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import object_types


def check_type(spec):
    """Verify one ObjectTypeSpec complies with the contract.

    Returns (success: bool, error_message: str or None).
    """
    try:
        # Build a synthetic item with the keys previews read
        item = {
            "slug": "selfcheck",
            "filename": f"selfcheck{list(spec.extensions)[0] if spec.extensions else ''}",
            "display_name": "Self-check <b>&",
            "media_type": spec.key,
            "type_label": spec.label,
            "icon": spec.badge_icon,
            "url": "/f/selfcheck",
            "thumb_url": "/f/selfcheck/thumb",
            "external_url": "https://example.com/watch?v=dQw4w9WgXcQ",
            "extracted_text": "line one\nline <two>",
            "content_description": "desc",
            "description": "",
            "type_metadata": {},
            "is_file": True,
            "stored_filename": None,
        }

        # Render preview in both modes
        for mode in ["live", "export"]:
            ctx = object_types.PreviewContext(
                item=item,
                media_url="/f/selfcheck",
                thumb_url="/f/selfcheck/thumb",
                page_url=item["external_url"],
                mode=mode,
                file_path=None,
            )
            preview = object_types.render_preview(spec, ctx)

            # Validate preview result
            if preview is not None:
                if not isinstance(preview, (str, markupsafe.Markup)):
                    return False, f"preview_fn returned {type(preview).__name__}, expected str/Markup"

                # Check for unescaped data (the synthetic "<b>&" should appear escaped)
                preview_str = str(preview)
                if "<b>&" in preview_str and not ("&lt;b&gt;&amp;" in preview_str or "&amp;lt;b&amp;gt;&amp;amp;" in preview_str):
                    # Allow either &lt;b&gt;&amp; or other safe escaping, but not raw <b>&
                    if "<b>" in preview_str or ">&" in preview_str:
                        return False, f"preview contains unescaped synthetic data: {preview_str[:100]}"

        # Call properties_fn
        props = spec.properties_fn(item)
        if not isinstance(props, dict):
            return False, f"properties_fn returned {type(props).__name__}, expected dict"
        for k, v in props.items():
            if not isinstance(k, str) or not isinstance(v, str):
                return False, f"properties_fn returned non-string key/value: {k}→{v}"

        return True, None

    except Exception as e:
        return False, f"{e!r}"


def main():
    """Run checks on all registered types."""
    print("Checking Constructicon object type registry (#448)…\n")

    # List hook declarations
    print("Registered types and their hooks:")
    print("-" * 80)
    for spec in sorted(object_types.OBJECT_TYPES.values(), key=lambda s: s.key):
        hooks = []
        if spec.preview_fn:
            hooks.append("preview_fn")
        if spec.properties_fn:
            hooks.append("properties_fn")
        if spec.sniff_fn:
            hooks.append("sniff_fn")
        if spec.pre_store_fn:
            hooks.append("pre_store_fn")
        if spec.url_match_fn:
            hooks.append("url_match_fn")
        if spec.actions:
            hooks.append(f"actions[{len(spec.actions)}]")
        if spec.edit_fields:
            hooks.append(f"edit_fields[{len(spec.edit_fields)}]")
        if spec.preview_assets:
            hooks.append(f"preview_assets[{len(spec.preview_assets)}]")
        if spec.writeup_body_key:
            hooks.append(f"writeup_body_key='{spec.writeup_body_key}'")
        if spec.embedded_metadata_fn:
            hooks.append("embedded_metadata_fn")

        hook_str = " ".join(hooks) if hooks else "(no optional hooks)"
        print(f"  {spec.key:20s} {spec.label:30s} {hook_str}")

    print("\n" + "-" * 80)
    print("Running compliance checks…\n")

    failures = []
    for spec in sorted(object_types.OBJECT_TYPES.values(), key=lambda s: s.key):
        success, error = check_type(spec)
        if success:
            print(f"✓ {spec.key}")
        else:
            print(f"✗ {spec.key}: {error}")
            failures.append((spec.key, error))

    print("\n" + "-" * 80)
    if failures:
        print(f"\nFAILED: {len(failures)} type(s) failed compliance checks:")
        for key, error in failures:
            print(f"  {key}: {error}")
        return 1
    else:
        print(f"\nOK: {len(object_types.OBJECT_TYPES)} types pass all checks.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
