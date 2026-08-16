from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("apply_assets", ROOT / "ops" / "apply-assets.py")
apply_assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply_assets)


def test_token_substitution_is_json_safe_and_manual_mode_commits_no_secret():
    template = (ROOT / "ops" / "custom.js").read_text()
    manual = apply_assets.render_javascript(template, None)
    assert 'const CONFIGURED_TOKEN = "__FEEDSUM_UI_TOKEN__";' in manual
    rendered = apply_assets.render_javascript(template, 'token"with\\syntax')
    assert 'const CONFIGURED_TOKEN = "token\\"with\\\\syntax";' in rendered
    assert 'token"with\\syntax' not in template


def test_css_preservation_splits_only_at_the_owned_marker():
    css = (ROOT / "ops" / "custom.css").read_text()
    existing = "body { color: red; }\n\n" + css.replace("#777", "#000")
    combined, kept_bytes = apply_assets.merge_stylesheet(existing, css)
    assert combined.startswith("body { color: red; }")
    assert kept_bytes == len("body { color: red; }")
    assert combined.count(apply_assets.MARKER) == 1
    assert "#777" in combined
