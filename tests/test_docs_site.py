"""The documentation site's own stylesheet and its phone layout.

Rendering can only be checked in a browser; these tests pin the two things the
phone layout depends on, so a change that undoes them fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / 'docs'


def _phone_block(css: str) -> str:
    match = re.search(r'@media screen and \(max-width: 767px\) \{(.*?)\n\}', css, re.S)
    assert match, 'site.css has no phone (max-width: 767px) block'
    return match.group(1)


def test_site_css_loads_after_the_theme():
    """Overrides in site.css share selectors with the theme's style.css; they
    only apply if site.css is loaded later."""
    layout = (DOCS / '_templates' / 'layout.html').read_text(encoding='utf-8')
    theme = layout.index('_static/style.css')
    site = layout.index('_static/site.css')
    assert site > theme
    # Loading it through html_css_files as well would put a copy before the theme
    conf = (DOCS / 'conf.py').read_text(encoding='utf-8')
    assert 'site.css' not in conf and 'landing.css' not in conf


def test_hero_is_a_single_column_on_phones():
    """On phones the hero's text column grew to the width of the unbreakable
    install command, so the tagline, badges and command were cut off at the
    right edge. Below 768px it is a block that fits the screen, and the
    command can wrap."""
    css = (DOCS / '_static' / 'site.css').read_text(encoding='utf-8')
    phone = _phone_block(css)
    hero = re.search(r'\.bp-hero \{(.*?)\}', phone, re.S)
    assert hero and 'display: block' in hero.group(1)
    install = re.search(r'\.bp-install \{(.*?)\}', phone, re.S)
    assert install and 'white-space: normal' in install.group(1)


def test_install_command_breaks_between_segments():
    index = (DOCS / 'index.rst').read_text(encoding='utf-8')
    command = re.search(r'<div class="bp-install">(.*?)</div>', index, re.S)
    assert command and '<wbr>' in command.group(1)
    assert 'better.py-self.git' in re.sub(r'<[^>]+>', '', command.group(1))
