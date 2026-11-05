"""tools/sync_wiki.py: wiki/ rendered for the GitHub Wiki."""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('sync_wiki', ROOT / 'tools' / 'sync_wiki.py')
assert _spec and _spec.loader
sync_wiki = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_wiki)

REPO = 'Demonic1594/better.py-self'
PAGES = {'README.md', 'gateway.md', 'events.md'}


def _rw(text: str) -> str:
    return sync_wiki.rewrite(text, PAGES, REPO, 'master')


@pytest.mark.parametrize(
    'source, expected',
    [
        ('[g](gateway.md)', '[g](gateway)'),
        ('[g](gateway.md#close-codes)', '[g](gateway#close-codes)'),
        ('[home](README.md)', '[home](Home)'),
        ('[here](#section)', '[here](#section)'),
        ('[x](https://example.com/a.md)', '[x](https://example.com/a.md)'),
        ('[api](../docs/api.rst)', f'[api](https://github.com/{REPO}/blob/master/docs/api.rst)'),
        ('[api](../docs/api.rst#events)', f'[api](https://github.com/{REPO}/blob/master/docs/api.rst#events)'),
        ('[ex](../examples)', f'[ex](https://github.com/{REPO}/tree/master/examples)'),
        ('[ci](../.github/workflows)', f'[ci](https://github.com/{REPO}/tree/master/.github/workflows)'),
        ('[`api.rst`](../docs/api.rst)', f'[`api.rst`](https://github.com/{REPO}/blob/master/docs/api.rst)'),
        ('![img](../docs/x.png)', f'![img](https://github.com/{REPO}/blob/master/docs/x.png)'),
        ('[unknown](other.md)', '[unknown](other.md)'),  # not a page here: left alone
        ('[out](../../elsewhere.md)', '[out](../../elsewhere.md)'),  # leaves the repo
        ('use `[g](gateway.md)` literally', 'use `[g](gateway.md)` literally'),
    ],
)
def test_rewrite_target(source, expected):
    assert _rw(source) == expected


def test_fenced_blocks_are_left_alone():
    text = '[g](gateway.md)\n```md\n[g](gateway.md)\n```\n[e](events.md)'
    assert _rw(text) == '[g](gateway)\n```md\n[g](gateway.md)\n```\n[e](events)'


def test_render_the_real_wiki(tmp_path):
    """Every page of wiki/ renders, and every link resolves to a page or to a
    file or folder that exists in the repository."""
    out = tmp_path / 'wiki'
    assert sync_wiki.main([str(ROOT / 'wiki'), str(out), '--repo', REPO]) == 0
    names = {p.stem for p in out.glob('*.md')}
    sources = {p.stem for p in (ROOT / 'wiki').glob('*.md')}
    assert names == (sources - {'README'}) | {'Home', '_Sidebar', '_Footer'}

    repo_url = re.compile(rf'https://github.com/{re.escape(REPO)}/(blob|tree)/master/?(.*)')
    unresolved = []
    for page in out.glob('*.md'):
        text = _prose(page.read_text(encoding='utf-8'), masked=True)
        assert '](../' not in text, page.name
        for match in sync_wiki._LINK.finditer(text):
            target = match.group(2)
            repo_link = repo_url.match(target)
            if repo_link:
                rel = ROOT / (repo_link.group(2).split('#')[0] or '.')
                is_file = repo_link.group(1) == 'blob'
                if not (rel.is_file() if is_file else rel.is_dir()):
                    unresolved.append((page.name, target))
            elif not target.startswith(('http://', 'https://', '#', 'mailto:')):
                if target.split('#')[0] not in names:
                    unresolved.append((page.name, target))
    assert unresolved == []


def test_stale_pages_are_removed_and_git_is_kept(tmp_path):
    out = tmp_path / 'wiki'
    (out / '.git').mkdir(parents=True)
    (out / 'Old-page.md').write_text('gone', encoding='utf-8')
    sync_wiki.main([str(ROOT / 'wiki'), str(out), '--repo', REPO])
    assert not (out / 'Old-page.md').exists()
    assert (out / '.git').is_dir() and (out / 'Home.md').is_file()


def test_missing_readme_is_an_error(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'page.md').write_text('# Page\n', encoding='utf-8')
    with pytest.raises(SystemExit):
        sync_wiki.render(src, REPO, 'master')


def test_sidebar_lists_every_page_by_title():
    rendered = sync_wiki.render(ROOT / 'wiki', REPO, 'master')
    sidebar = rendered['_Sidebar.md']
    for name in rendered:
        if name.startswith('_') or name == 'Home.md':
            continue
        assert f']({name[:-3]})' in sidebar
    assert os.sep not in sidebar


def test_blob_or_tree_comes_from_the_filesystem(tmp_path):
    root = tmp_path / 'repo'
    (root / 'docs' / 'v2.3').mkdir(parents=True)
    (root / 'LICENSE').write_text('x', encoding='utf-8')
    rw = lambda t: sync_wiki.rewrite(t, PAGES, REPO, 'master', root)  # noqa: E731
    assert rw('[l](../LICENSE)') == f'[l](https://github.com/{REPO}/blob/master/LICENSE)'
    assert rw('[d](../docs/v2.3)') == f'[d](https://github.com/{REPO}/tree/master/docs/v2.3)'


def test_unknown_md_link_warns(capsys):
    assert _rw('[x](./gateway.md)') == '[x](./gateway.md)'
    assert 'not a page' in capsys.readouterr().err


def test_title_skips_fenced_blocks():
    assert sync_wiki.title_of('```sh\n# install\n```\n# Real title\n', 'x') == 'Real title'


def _slug(heading: str) -> str:
    # GitHub's heading anchors: lower-case, punctuation dropped, spaces to hyphens
    heading = re.sub(r'`|\*\*|\*', '', heading).strip().lower()
    return re.sub(r'[^\w\- ]', '', heading).replace(' ', '-')


def _prose(text: str, *, masked: bool = False) -> str:
    # The prose outside fenced blocks, with the same rules as the sync. Blocks
    # are joined by a blank line, so neither a link nor a code span joins two.
    chunks = [chunk for prose, chunk in sync_wiki.blocks(text) if prose]
    return '\n\n'.join(sync_wiki.mask_code(c) if masked else c for c in chunks)


def _missing_anchors(pages):
    """``(page, target)`` for every ``#anchor`` link in ``pages`` (name -> text)
    that matches no heading. Fenced code and inline code are skipped; links may
    span lines."""
    prose = {name: _prose(text) for name, text in pages.items()}
    anchors = {name: {_slug(m.group(1)) for m in re.finditer(r'^#+ (.+)$', text, re.M)} for name, text in prose.items()}
    missing = []
    for name, text in prose.items():
        for match in sync_wiki._LINK.finditer(sync_wiki.mask_code(text)):
            target = match.group(2)
            if target.startswith(('http://', 'https://')) or '#' not in target:
                continue
            page, _, anchor = target.partition('#')
            if anchor not in anchors.get(page or name, set()):
                missing.append((name, target))
    return missing


def test_anchors_point_at_headings(tmp_path):
    out = tmp_path / 'wiki'
    sync_wiki.main([str(ROOT / 'wiki'), str(out), '--repo', REPO])
    assert _missing_anchors({page.stem: page.read_text(encoding='utf-8') for page in out.glob('*.md')}) == []


def test_anchor_check_skips_fences_and_follows_multiline_links():
    page = '# Real\n```md\n# Sample\n[go](#sample)\n```\n[ok](#real)\n[split\ntext](#missing)'
    assert _missing_anchors({'Home': page}) == [('Home', '#missing')]


def test_anchor_check_skips_inline_code():
    page = (
        '# Real\n'
        'Write `[go](#sample)` or ``[a `b`](#x)`` literally, even `[w\nrapped](#y)`.\n'
        '[ok](#real) [`code` text](#gone)\n'
        '\n'
        'A stray ` here\n'
        '\n'
        '[after](#missing) and ` there'
    )
    # The stray backticks sit in different paragraphs, so they don't pair up
    # and hide the broken link between them
    assert _missing_anchors({'Home': page}) == [('Home', '#gone'), ('Home', '#missing')]


def test_prose_skips_fenced_examples():
    text = '# Real\n```md\n# Sample\n[go](#sample)\n```\n[ok](#real)'
    assert _prose(text) == '# Real\n\n[ok](#real)'
    assert sync_wiki.blocks(text) == [
        (True, '# Real'),
        (False, '```md\n# Sample\n[go](#sample)\n```'),
        (True, '[ok](#real)'),
    ]


def test_rewrite_follows_links_across_lines():
    text = 'See the [api\nreference](../docs/api.rst) and [the\ngateway](gateway.md#x).'
    expected = f'See the [api\nreference](https://github.com/{REPO}/blob/master/docs/api.rst) and [the\ngateway](gateway#x).'
    assert _rw(text) == expected


def test_rewrite_skips_code_spans_that_wrap():
    text = '`[api\nreference](../docs/api.rst)` and ``[g](gateway.md)``\n\nA stray ` then [g](gateway.md)\n\nand ` here'
    expected = '`[api\nreference](../docs/api.rst)` and ``[g](gateway.md)``\n\nA stray ` then [g](gateway)\n\nand ` here'
    assert _rw(text) == expected
    # A span that opens on one line and closes on the next hides the link between
    literal = 'Write `the link\n[g](gateway.md)` as is.'
    assert _rw(literal) == literal


def test_longer_fence_holds_a_shorter_one():
    text = '````md\n```\n[g](gateway.md)\n# not a title\n```\n````\n# Title\n[e](events.md)'
    assert _rw(text) == '````md\n```\n[g](gateway.md)\n# not a title\n```\n````\n# Title\n[e](events)'
    assert sync_wiki.title_of(text, 'x') == 'Title'
    # A tilde fence isn't closed by backticks
    assert _rw('~~~\n```\n[g](gateway.md)\n~~~\n[g](gateway.md)') == '~~~\n```\n[g](gateway.md)\n~~~\n[g](gateway)'
