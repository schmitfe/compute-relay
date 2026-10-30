"""Source previews expose ambiguous bytes without changing executable files."""

from html import unescape

import pytest
from django.test import Client
from django.utils.html import strip_tags

from compute_relay.control import services
from compute_relay.control.review_text import decode_preview, prepare_review_files, visible_text
from compute_relay.control.storage import read_blob, store_uploaded_blob
from test_support.unit_test_control import broker, prepared_job


@pytest.mark.parametrize("character", ["\u2028", "\u2029", "\u115f", "\u1160", "\u3164", "\uffa0", "\u00a0", "\ud800", "\u0378"])
def test_nonprinting_source_characters_are_explicit(character):
    text, hidden = visible_text("before" + character + "after")
    assert hidden and f"U+{ord(character):04X}" in text
    assert character not in text


def test_ordinary_unicode_and_indentation_remain_readable():
    assert visible_text("\n\tprint('café λ 日本語')\n") == ("\n\tprint('café λ 日本語')\n", False)
    assert decode_preview(b"# bad byte: \xff\n") == {"text": "# bad byte: \\xff\n", "invalid_utf8": True}
    assert decode_preview("print('café')".encode()) == {"text": "print('café')", "invalid_utf8": False}


@pytest.mark.parametrize("path,text", [
    ("main.py", "\n\tprint('<script>alert(1)</script>')"),
    ("run.sh", "#!/bin/bash\nprintf '%s' '<img src=x onerror=alert(1)>'\n\n"),
    ("input.json", '{"value": "<script>"}'),
    ("main.py", "print('safe') #\u202eevil\u2029\n"),
])
def test_highlighting_escapes_markup_and_preserves_visible_source(path, text):
    files = [{"path": path, "text": text}]
    styles = prepare_review_files(files)
    file = files[0]
    assert ".source-preview" in styles
    assert "<span" in file["review_html"]
    assert "<script>" not in file["review_html"] and "<img " not in file["review_html"]
    assert unescape(strip_tags(str(file["review_html"]))) == file["review_text"]


@pytest.mark.parametrize("path,text", [("notes.txt", "plain <text>"), ("big.py", "#" * (128 * 1024 + 1)),
                                      ("unicode.py", "λ" * (64 * 1024 + 1))])
def test_unknown_and_large_sources_use_plain_text(path, text):
    files = [{"path": path, "text": text}]
    prepare_review_files(files)
    assert files[0]["review_text"] == text and "review_html" not in files[0]


def test_job_review_warns_about_invalid_utf8_and_keeps_original_bytes(broker):
    job = prepared_job(broker)
    content = b"print('<script>') # invalid: \xff\n"
    blob = store_uploaded_blob(broker.project, content)
    job.files.filter(path="main.py").update(blob=blob)
    package = services.serialize_job(job, package=True)
    preview = next(file for file in package["files"] if file["path"] == "main.py")
    assert preview["invalid_utf8"] and "\\xff" in preview["text"]
    browser = Client()
    browser.force_login(broker.owner)
    page = browser.get(f"/review/{job.id}/")
    assert page.status_code == 200
    assert b"not valid UTF-8" in page.content and b"\\xff" in page.content
    block = next(part.split(b"</pre>", 1)[0] for part in page.content.split(b'<pre class="source-preview">')[1:]
                 if b"\\xff" in part.split(b"</pre>", 1)[0])
    assert b"&lt;script&gt;" in block and b"<script>" not in block
    assert read_blob(blob) == content and preview["sha256"] == blob.sha256
