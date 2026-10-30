"""Visible representations of invisible characters at the human review gate."""

import unicodedata
from pathlib import PurePosixPath


def decode_preview(content):
    try:
        return {"text": content.decode("utf-8"), "invalid_utf8": False}
    except UnicodeDecodeError:
        return {"text": content.decode("utf-8", errors="backslashreplace"), "invalid_utf8": True}


def visible_text(text):
    hidden = False
    parts = []
    for char in text:
        code = ord(char)
        if (not char.isprintable() and char not in "\n\t"
                or code in {0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0}
                or 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF):
            parts.append(f"[U+{code:04X} {unicodedata.name(char, 'UNNAMED')}]")
            hidden = True
        else:
            parts.append(char)
    return "".join(parts), hidden


def prepare_review_files(files):
    from django.utils.safestring import mark_safe
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import BashLexer, JsonLexer, PythonLexer

    formatter = HtmlFormatter(nowrap=True, style="native", classprefix="src-")
    lexers = {".py": PythonLexer, ".sh": BashLexer, ".bash": BashLexer, ".json": JsonLexer}
    for file in files:
        file["review_path"], path_hidden = visible_text(file["path"])
        file["hidden_characters"] = path_hidden
        if file["text"] is not None:
            file["review_text"], hidden = visible_text(file["text"])
            file["hidden_characters"] |= hidden
            lexer = lexers.get(PurePosixPath(file["path"]).suffix.lower())
            if lexer is not None and len(file["review_text"].encode("utf-8")) <= 128 * 1024:
                rendered = highlight(file["review_text"], lexer(stripnl=False, ensurenl=False), formatter)
                if not file["review_text"].endswith("\n"):
                    rendered = rendered.removesuffix("\n")
                # Only the highlighter's escaped spans are trusted HTML.
                file["review_html"] = mark_safe(rendered)
    return formatter.get_style_defs(".source-preview")
