"""
Render the repository's ``wiki/`` folder for the GitHub Wiki.

``wiki/`` is the source of truth: it is reviewed in pull requests like the
rest of the repository. The GitHub Wiki is a separate git repository whose
pages are addressed by name, so the files need three changes on the way:

- ``README.md`` becomes ``Home.md`` (the wiki's front page);
- links to other pages lose their ``.md`` (``gateway.md#x`` -> ``gateway#x``,
  ``README.md`` -> ``Home``);
- links that leave the folder (``../docs/api.rst``) point at the file on
  GitHub, since the wiki can't reach the repository's files.

It also writes ``_Sidebar.md`` (every page, by its title) and ``_Footer.md``
(where to edit). ``.github/workflows/wiki.yml`` runs it on every push to
master that touches ``wiki/`` and pushes the result to ``<repo>.wiki.git``.

Usage: ``python tools/sync_wiki.py SRC DEST --repo OWNER/NAME [--ref master]``.
DEST's existing ``*.md`` files are replaced, so pages deleted from ``wiki/``
are deleted from the wiki too; anything else in DEST (``.git``) is kept.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

# Inline links and images: [text](target) / ![alt](target). Reference-style
# links aren't used in wiki/; code spans are skipped below.
_LINK = re.compile(r'(!?\[[^\]]*\])\(([^()\s]+)\)')
_FENCE = re.compile(r'^(`{3,}|~{3,})')


def _next_fence(line: str, fence: Optional[str]) -> Optional[str]:
    """The open fence marker after ``line``, or None outside a fenced block.

    A block closes only on a marker of the same character that is at least
    as long as the opening one, with nothing after it (CommonMark), so a
    ````-block can show a ```-example.
    """
    stripped = line.lstrip()
    match = _FENCE.match(stripped)
    if match is None:
        return fence
    marker = match.group(1)
    if fence is None:
        return marker
    closes = marker[0] == fence[0] and len(marker) >= len(fence) and not stripped[len(marker) :].strip()
    return None if closes else fence


# An inline code span (CommonMark): a backtick run closed by the next run of
# exactly the same length. It may wrap lines but never crosses a blank line.
_CODE_SPAN = re.compile(r'(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)', re.S)
_BLANK_LINE = re.compile(r'\n[ \t]*\n')


def code_spans(text: str) -> List[Tuple[int, int]]:
    """``(start, end)`` of every inline code span in ``text``, one paragraph at
    a time so that stray backticks in different paragraphs never pair up."""
    spans: List[Tuple[int, int]] = []
    start = 0
    for brk in [*_BLANK_LINE.finditer(text), None]:
        end = brk.start() if brk else len(text)
        spans += [m.span() for m in _CODE_SPAN.finditer(text, start, end)]
        if brk:
            start = brk.end()
    return spans


def mask_code(text: str) -> str:
    """``text`` with inline code blanked out, keeping offsets and line breaks."""
    chars = list(text)
    for a, b in code_spans(text):
        chars[a:b] = [c if c == '\n' else ' ' for c in text[a:b]]
    return ''.join(chars)


def blocks(text: str) -> List[Tuple[bool, str]]:
    """``text`` split into ``(is_prose, chunk)`` runs of whole lines: prose, or
    a fenced block with its fences. Joining the chunks with ``\\n`` restores it."""
    out: List[Tuple[bool, str]] = []
    run: List[str] = []
    run_prose = True
    fence: Optional[str] = None
    for line in text.split('\n'):
        new = _next_fence(line, fence)
        prose = fence is None and new is None
        if prose != run_prose and run:
            out.append((run_prose, '\n'.join(run)))
            run = []
        run_prose = prose
        run.append(line)
        fence = new
    out.append((run_prose, '\n'.join(run)))
    return out


def page_name(filename: str) -> str:
    stem = filename[:-3] if filename.endswith('.md') else filename
    return 'Home' if stem == 'README' else stem


def rewrite_target(target: str, pages: set, repo: str, ref: str, root: Optional[Path] = None) -> str:
    if target.startswith(('http://', 'https://', 'mailto:', '#')):
        return target
    path, _, anchor = target.partition('#')
    suffix = f'#{anchor}' if anchor else ''
    if '/' not in path and path in pages:
        return page_name(path) + suffix
    if path.startswith('../'):
        rel = PurePosixPath(path[3:])
        if '..' in rel.parts:
            return target  # leaves the repository: leave it alone
        if root is not None:
            # GitHub wants /tree/ for folders and /blob/ for files
            kind = 'tree' if (root / str(rel)).is_dir() else 'blob'
        else:
            kind = 'blob' if rel.suffix else 'tree'
        rel_str = '' if str(rel) == '.' else str(rel)
        return f'https://github.com/{repo}/{kind}/{ref}/{rel_str}'.rstrip('/') + suffix
    if path.endswith('.md'):
        # e.g. ./page.md or a page that doesn't exist: it would 404 on the wiki
        print(f'warning: link to {target!r} is not a page in wiki/; left as is', file=sys.stderr)
    return target


def rewrite_prose(prose: str, pages: set, repo: str, ref: str, root: Optional[Path] = None) -> str:
    # Links may span lines. Leave links that start inside inline code alone;
    # a code span inside a link's text (``[`api.rst`](../docs/api.rst)``) is fine
    spans = code_spans(prose)

    def replace(match: re.Match[str]) -> str:
        start = match.start()
        if any(a <= start < b for a, b in spans):
            return match.group(0)
        return f'{match.group(1)}({rewrite_target(match.group(2), pages, repo, ref, root)})'

    return _LINK.sub(replace, prose)


def rewrite(text: str, pages: set, repo: str, ref: str, root: Optional[Path] = None) -> str:
    return '\n'.join(rewrite_prose(chunk, pages, repo, ref, root) if prose else chunk for prose, chunk in blocks(text))


def title_of(text: str, fallback: str) -> str:
    fence: Optional[str] = None
    for line in text.split('\n'):
        new = _next_fence(line, fence)
        if fence is None and new is None and line.startswith('# '):
            return line[2:].strip()
        fence = new
    return fallback


def render(src: Path, repo: str, ref: str) -> Dict[str, str]:
    sources = sorted(p for p in src.glob('*.md'))
    if not any(p.name == 'README.md' for p in sources):
        raise SystemExit(f'{src}/README.md is missing: it becomes the wiki Home page')
    pages = {p.name for p in sources}
    rendered: Dict[str, str] = {}
    titles: Dict[str, str] = {}
    for path in sources:
        text = path.read_text(encoding='utf-8')
        name = page_name(path.name)
        rendered[f'{name}.md'] = rewrite(text, pages, repo, ref, src.resolve().parent)
        titles[name] = title_of(text, name)

    sidebar = ['**[Home](Home)**', '']
    sidebar += [f'- [{titles[n]}]({n})' for n in sorted(titles, key=lambda n: titles[n].lower()) if n != 'Home']
    rendered['_Sidebar.md'] = '\n'.join(sidebar) + '\n'
    rendered['_Footer.md'] = (
        f'Generated from [`wiki/`](https://github.com/{repo}/tree/{ref}/wiki) in the repository. '
        'Edit the files there in a pull request; edits made here are overwritten on the next sync.\n'
    )
    return rendered


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('src', type=Path)
    parser.add_argument('dest', type=Path)
    parser.add_argument('--repo', required=True, help='OWNER/NAME, for links back to the repository')
    parser.add_argument('--ref', default='master', help='branch the repository links point at')
    args = parser.parse_args(argv)

    rendered = render(args.src, args.repo, args.ref)
    args.dest.mkdir(parents=True, exist_ok=True)
    for old in args.dest.glob('*.md'):
        if old.name not in rendered:
            old.unlink()
            print(f'removed {old.name}')
    for name, text in rendered.items():
        target = args.dest / name
        if not target.exists() or target.read_text(encoding='utf-8') != text:
            target.write_text(text, encoding='utf-8')
            print(f'wrote {name}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
