"""Browser-local appearance stays accessible without touching broker authority."""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess

from django.template.loader import render_to_string
import pytest


TEMPLATES = Path(__file__).resolve().parents[2] / "src/compute_relay/control/templates/broker"


class Markup(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_base_initializes_theme_before_styles_and_offers_a_labelled_native_selector():
    html = render_to_string("broker/base.html")
    assert html.index("compute-relay.theme") < html.index("<style>") < html.index("<body>")
    tags = Markup(html).tags
    assert ("label", {"for": "portal-theme"}) in tags
    assert ("select", {"id": "portal-theme"}) in tags
    assert [attrs["value"] for tag, attrs in tags if tag == "option"] == ["system", "light", "dark"]
    assert ("meta", {"name": "color-scheme", "content": "light dark"}) in tags


@pytest.fixture
def browser():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node is optional; browser appearance behavior requires its JavaScript runtime")
    script = (TEMPLATES / "portal_theme_script.html").read_text().removeprefix("<script>\n").removesuffix("</script>\n")
    harness = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const events = {}, documentEvents = {}, selectorEvents = {};
const values = Object.assign({}, input.stored === undefined ? {} : {'compute-relay.theme': input.stored});
const writes = [];
const root = {dataset: {}, style: {}};
const picker = {hidden: true};
const selector = {value: 'system', addEventListener(type, handler) { selectorEvents[type] = handler; }};
const media = {matches: Boolean(input.dark)};
media.addEventListener = (type, handler) => { events.media = handler; };
const window = {matchMedia() { return media; }, addEventListener(type, handler) { events[type] = handler; }};
Object.defineProperty(window, 'localStorage', {get() {
  if (input.blockStorage) throw new Error('Storage unavailable');
  return {getItem(key) { return values[key] ?? null; }, setItem(key, value) {
    if (input.blockWrites) throw new Error('Storage quota exceeded');
    values[key] = value; writes.push([key, value]);
  }};
}});
const document = {documentElement: root,
  addEventListener(type, handler) { documentEvents[type] = handler; },
  getElementById(id) { return id === 'portal-theme' ? selector : id === 'portal-theme-picker' ? picker : null; }};
vm.runInNewContext(input.script, {window, document});
function state() { return {theme: root.dataset.theme, preference: root.dataset.themePreference,
  colorScheme: root.style.colorScheme, selected: selector.value, visible: !picker.hidden}; }
const initial = state();
documentEvents.DOMContentLoaded();
const ready = state(), states = [];
for (const action of input.actions || []) {
  if (action.kind === 'choose') { selector.value = action.value; selectorEvents.change(); }
  else if (action.kind === 'system') { media.matches = action.dark; events.media(); }
  else if (action.kind === 'storage') events.storage({key: action.key ?? 'compute-relay.theme', newValue: action.value});
  states.push(state());
}
process.stdout.write(JSON.stringify({initial, ready, states, values, writes}));
"""

    def run(**config):
        result = subprocess.run([executable, "-e", harness], input=json.dumps({"script": script, **config}),
            text=True, capture_output=True, check=True, timeout=10)
        return json.loads(result.stdout)

    return run


@pytest.mark.parametrize("stored,dark,theme,preference", [
    (None, False, "light", "system"), (None, True, "dark", "system"),
    ("light", True, "light", "light"), ("dark", False, "dark", "dark"),
    ("system", True, "dark", "system"), ("unrecognized", True, "dark", "system"),
])
def test_first_paint_uses_a_valid_saved_choice_or_the_system_before_dom_ready(browser, stored, dark, theme, preference):
    result = browser(stored=stored, dark=dark)
    assert result["initial"]["theme"] == result["initial"]["colorScheme"] == theme
    assert result["initial"]["preference"] == preference
    assert result["ready"]["selected"] == preference and result["ready"]["visible"] is True
    assert result["writes"] == []


def test_user_choice_persists_and_explicit_theme_ignores_later_system_changes(browser):
    result = browser(actions=[{"kind": "choose", "value": "dark"}, {"kind": "system", "dark": False},
        {"kind": "choose", "value": "light"}, {"kind": "system", "dark": True}])
    assert [state["theme"] for state in result["states"]] == ["dark", "dark", "light", "light"]
    assert result["writes"] == [["compute-relay.theme", "dark"], ["compute-relay.theme", "light"]]
    assert result["values"]["compute-relay.theme"] == "light"


def test_system_choice_follows_live_operating_system_changes(browser):
    result = browser(stored="light", actions=[{"kind": "choose", "value": "system"},
        {"kind": "system", "dark": True}, {"kind": "system", "dark": False}])
    assert [state["theme"] for state in result["states"]] == ["light", "dark", "light"]
    assert all(state["selected"] == "system" for state in result["states"])


@pytest.mark.parametrize("failure", ["blockStorage", "blockWrites"])
def test_blocked_or_full_storage_still_allows_using_the_selector(browser, failure):
    result = browser(dark=True, actions=[{"kind": "choose", "value": "light"}], **{failure: True})
    assert result["initial"]["theme"] == "dark"
    assert result["states"][0]["theme"] == "light" and result["states"][0]["selected"] == "light"
    assert result["writes"] == []


def test_other_tabs_theme_changes_sync_but_unrelated_storage_does_not_affect_the_portal(browser):
    result = browser(actions=[{"kind": "storage", "value": "dark"},
        {"kind": "storage", "key": "other.application", "value": "light"},
        {"kind": "storage", "value": None}])
    assert [state["theme"] for state in result["states"]] == ["dark", "dark", "light"]
    assert result["states"][-1]["preference"] == "system" and result["writes"] == []


def palettes():
    styles = (TEMPLATES / "portal_styles.html").read_text()
    blocks = re.findall(r":root(?:\[data-theme=\"dark\"\])?\{([^}]+)\}", styles)
    return [dict(re.findall(r"--([a-z-]+):(#\w{6})", block)) for block in blocks]


def contrast(first, second):
    def luminance(color):
        values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        channels = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055)**2.4 for value in values]
        return sum(value * weight for value, weight in zip(channels, (.2126, .7152, .0722)))

    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + .05) / (dark + .05)


@pytest.mark.parametrize("palette", [0, 1], ids=["light", "dark"])
@pytest.mark.parametrize("foreground,background", [
    ("text", "page-bg"), ("text", "surface-bg"), ("muted", "surface-bg"), ("link", "surface-bg"),
    ("control-text", "control-bg"), ("placeholder", "control-bg"), ("code-text", "code-bg"),
    ("danger", "surface-bg"), ("danger", "error-bg"), ("notice-text", "notice-bg"),
    ("button-text", "button-bg"), ("button-text", "button-hover"), ("danger-text", "danger-bg"),
    ("link", "secondary-bg"), ("text", "badge-bg"), ("disabled-text", "disabled-bg"),
])
def test_portal_text_controls_notices_and_logs_keep_readable_palette_contrast(palette, foreground, background):
    colors = palettes()[palette]
    assert contrast(colors[foreground], colors[background]) >= 4.5


def test_dark_system_fallback_matches_explicit_dark_and_focus_is_visible():
    styles = (TEMPLATES / "portal_styles.html").read_text()
    dark = palettes()[1]
    fallback = styles.split("@media(prefers-color-scheme:dark)", 1)[1].split("}}", 1)[0]
    assert dict(re.findall(r"--([a-z-]+):(#\w{6})", fallback)) == dark
    for colors in palettes():
        assert contrast(colors["focus"], colors["surface-bg"]) >= 3
        assert contrast(colors["control-border"], colors["control-bg"]) >= 3
