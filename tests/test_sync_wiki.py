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
        text = page.read_text(encoding='utf-8')
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


def test_anchors_point_at_headings(tmp_path):
    out = tmp_path / 'wiki'
    sync_wiki.main([str(ROOT / 'wiki'), str(out), '--repo', REPO])
    anchors = {}
    for page in out.glob('*.md'):
        text = page.read_text(encoding='utf-8')
        anchors[page.stem] = {_slug(h) for h in re.findall(r'^#+ (.+)$', text, re.M)}
    missing = []
    for page in out.glob('*.md'):
        for match in sync_wiki._LINK.finditer(page.read_text(encoding='utf-8')):
            target = match.group(2)
            if target.startswith(('http://', 'https://')) or '#' not in target:
                continue
            name, _, anchor = target.partition('#')
            if anchor not in anchors.get(name or page.stem, set()):
                missing.append((page.name, target))
    assert missing == []
