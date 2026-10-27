"""Visible representations of invisible characters at the human review gate."""

import unicodedata


def visible_text(text):
    hidden = False
    parts = []
    for char in text:
        code = ord(char)
        if (unicodedata.category(char) in {"Cf", "Cc"} and char not in "\n\t"
                or code == 0x034F or 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF):
            parts.append(f"[U+{code:04X} {unicodedata.name(char, 'CONTROL')}]")
            hidden = True
        else:
            parts.append(char)
    return "".join(parts), hidden


def prepare_review_files(files):
    for file in files:
        file["review_path"], path_hidden = visible_text(file["path"])
        file["hidden_characters"] = path_hidden
        if file["text"] is not None:
            file["review_text"], hidden = visible_text(file["text"])
            file["hidden_characters"] |= hidden
