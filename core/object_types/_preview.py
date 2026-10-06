"""Shared building blocks for type preview_fn's (#449).

Every helper returns markupsafe.Markup, escapes all data (use
markupsafe.escape / Markup.format), and is mode-aware (ctx.mode "live" =
the object page, "export" = the static site).
"""

from markupsafe import Markup, escape


def _rotation_style(item):
    """#266 display rotation as a style attribute, or "". type_metadata is
    writable over the API, so never interpolate it raw into an attribute:
    only the four real rotations are emitted, as integers."""
    try:
        deg = int((item.get("type_metadata") or {}).get("rotation") or 0)
    except (TypeError, ValueError):  # silent-ok: an unusable rotation is "no rotation"; the value is never echoed
        return ""
    return f' style="transform:rotate({deg}deg)"' if deg in (90, 180, 270) else ""


def _alt(item):
    """Best human label; `or`, not .get(k, default), because keys can hold None."""
    return item.get("display_name") or item.get("filename") or item.get("slug") or ""


def image_viewer(ctx):
    """Live: <img id="preview-img"> with optional rotation style (alt = filename,
    as the template had it). Export: <img class="item-media"> without id/rotation."""
    item = ctx.item
    if ctx.mode == "live":
        return Markup(
            f'<img id="preview-img" src="{escape(ctx.media_url)}" alt="{escape(item.get("filename") or "")}" '
            f'title="Click to view full size"{_rotation_style(item)}>'
        )
    return Markup(f'<img src="{escape(ctx.media_url)}" alt="{escape(_alt(item))}" class="item-media">')


def thumb_with_original_link(ctx):
    """Live: <div class="capture-preview"> with img and optional "View original" link.
    Export: linked or bare image depending on media_url."""
    item = ctx.item
    alt = _alt(item)
    if ctx.mode == "live":
        html = (f'<div class="capture-preview"><img id="preview-img" src="{escape(ctx.thumb_url)}" '
                f'alt="{escape(alt)}" title="Click to view full size"{_rotation_style(item)}>')
        if ctx.media_url:
            html += (f'<a href="{escape(ctx.media_url)}" class="external-link-btn">'
                     f'View original {escape(item.get("type_label") or "file")}</a>')
        return Markup(html + "</div>")
    img = f'<img src="{escape(ctx.thumb_url)}" alt="{escape(alt)}" class="item-media">'
    return Markup(f'<a href="{escape(ctx.media_url)}">{img}</a>' if ctx.media_url else img)


def file_icon(ctx):
    """Live: generic 64x64 file SVG.
    Export: link to file or empty string."""
    media_url = ctx.media_url
    alt = _alt(ctx.item)

    if ctx.mode == "live":
        return Markup(
            '<svg width="64" height="64" viewBox="0 0 24 24" fill="none">'
            '<rect x="3" y="4" width="18" height="16" rx="2" stroke="#3F4530" stroke-width="1.4"/>'
            '<circle cx="8" cy="9" r="1.6" stroke="#3F4530" stroke-width="1.4"/>'
            '<path d="M3 16L8.5 11.5L13 15L16.5 12L21 16" stroke="#3F4530" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>'
            '</svg>'
        )
    else:  # export
        if media_url:
            return Markup(f'<a href="{escape(media_url)}">{escape(alt)}</a>')
        else:
            return Markup("")


def video_player(ctx):
    """Live: <div class="video-preview"><video> with poster.
    Export: bare <video>."""
    media_url = ctx.media_url
    thumb_url = ctx.thumb_url

    if ctx.mode == "live":
        return Markup(
            f'<div class="video-preview"><video controls preload="metadata" '
            f'poster="{escape(thumb_url)}" src="{escape(media_url)}"></video></div>'
        )
    else:  # export
        return Markup(
            f'<video src="{escape(media_url)}" controls preload="metadata" class="item-media"></video>'
        )


def audio_player(ctx):
    """Live: <div class="audio-preview"><div class="audio-preview-icon"> with audio.
    Export: bare <audio>."""
    media_url = ctx.media_url
    icon = ctx.item.get("icon") or "🎵"

    if ctx.mode == "live":
        return Markup(
            f'<div class="audio-preview"><div class="audio-preview-icon">{escape(icon)}</div>'
            f'<audio controls preload="metadata" src="{escape(media_url)}"></audio></div>'
        )
    else:  # export
        return Markup(
            f'<audio src="{escape(media_url)}" controls preload="metadata"></audio>'
        )


def youtube_embed(ctx, embed_url):
    """Live: <div class="video-embed-wrap"><iframe>.
    Export: <div class="youtube-embed"><iframe> with width/height."""
    display_name = _alt(ctx.item)

    if ctx.mode == "live":
        return Markup(
            f'<div class="video-embed-wrap"><iframe src="{escape(embed_url)}" '
            f'title="{escape(display_name)}" allowfullscreen loading="lazy"></iframe></div>'
        )
    else:  # export
        return Markup(
            f'<div class="youtube-embed"><iframe width="560" height="315" '
            f'src="{escape(embed_url)}" frameborder="0" allowfullscreen></iframe></div>'
        )


def link_card(ctx, url, label=None):
    """Live: <div class="content-text-preview"><a> external link button.
    Export: bare <a>."""
    link_text = escape(label or url)

    if ctx.mode == "live":
        return Markup(
            f'<div class="content-text-preview"><a href="{escape(url)}" target="_blank" '
            f'rel="noopener" class="external-link-btn">{link_text}</a></div>'
        )
    else:  # export
        return Markup(f'<a href="{escape(url)}" target="_blank">{link_text}</a>')


def text_block(ctx, text):
    """Live: <div class="content-text-preview"><p>.
    Export: bare <p>."""
    safe_text = escape(text)

    if ctx.mode == "live":
        return Markup(f'<div class="content-text-preview"><p>{safe_text}</p></div>')
    else:  # export
        return Markup(f'<p>{safe_text}</p>')
