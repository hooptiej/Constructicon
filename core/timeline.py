"""Pure effective-date resolution for the Timeline feature — no DB access.
See docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.

Objects (capture_events rows) are a single point in time. Projects are a
span: they took time, they aren't moments.
"""


def resolve_item_date(row):
    """row is a capture_events dict (core.db.get_by_slug/_row_to_dict
    shape). timestamp is NOT NULL in the schema, so this always resolves."""
    if row.get("display_date_override") is not None:
        return row["display_date_override"]
    if row.get("content_date") is not None:
        return row["content_date"]
    return row["timestamp"]


def resolve_project_span(project, items):
    """project is a core.db projects dict. items is that project's
    capture_events rows (core.db.list_project_items shape) — may be empty.
    Returns (start, end); an empty or single-moment project collapses to
    start == end, deliberately, rather than a special-cased "no span" branch.

    The write-up document (project["writeup_slug"], when it's also one of
    `items` — #156 projects can include their write-up in project_items
    like any other member) is excluded from the span calculation. A
    write-up's own timestamp is whenever it was authored/generated, not
    when the project's actual content happened — write-ups are being
    machine-generated fresh for existing projects, which would otherwise
    drag every project's effective_end to "whenever we wrote it up,"
    masking the real chronology entirely."""
    start_override = project.get("start_date_override")
    end_override = project.get("end_date_override")
    if start_override is not None and end_override is not None:
        return start_override, end_override

    writeup_slug = project.get("writeup_slug")
    dated_items = [item for item in items if item["slug"] != writeup_slug]
    item_dates = [resolve_item_date(item) for item in dated_items]
    derived_start = min(item_dates) if item_dates else project["created_at"]
    derived_end = max(item_dates) if item_dates else project["created_at"]

    start = start_override if start_override is not None else derived_start
    end = end_override if end_override is not None else derived_end
    return start, end
