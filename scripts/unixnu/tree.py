"""Assemble the build tree from the resolved modules.

out/<device>/tree/ has the layout of this repository (drivers/ kits/ patches/ scripts/ package/)
plus refer/, build/ and .cache/ for the scripts that expect them. Modules are symbolic links to
their sources, so edits to a local module take effect on the next build; scripts are copied, since
they locate the tree from their own real path. The device package appears as kits/<kit>.
"""
import shutil
from pathlib import Path

from .util import Error, log


def _clear_links(directory):
    if directory.is_dir():
        for entry in directory.iterdir():
            if entry.is_symlink():
                entry.unlink()


def _link(target, link):
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(Path(target).resolve())


def _copy_scripts(source, destination):
    for path in sorted(Path(source).rglob('*')):
        relative = path.relative_to(source)
        if path.is_dir() or '__pycache__' in path.parts or relative.parts[0] == 'unixnu':
            continue
        target = destination / relative
        if target.exists() and target.stat().st_size == path.stat().st_size and target.stat().st_mtime >= path.stat().st_mtime:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def assemble(ctx, modules):
    log(f'tree {ctx.tree}')
    tree = ctx.tree
    tree.mkdir(parents=True, exist_ok=True)
    for folder in ('drivers', 'kits', 'patches', 'refer'):
        _clear_links(tree / folder)
        (tree / folder).mkdir(exist_ok=True)
    for name in ('build', '.cache'):
        (tree / name).mkdir(exist_ok=True)
    kit = ctx.board.get('board', {}).get('kit', ctx.name)
    for (kind, name), module in sorted(modules.items()):
        if module.path is None:
            if kind == 'refer':
                # A refer source that has already been fetched is linked now, so "unixnu tree" alone
                # produces a complete tree; one that is not fetched yet is left to the step that needs it.
                # feeds._clone_dir owns the checkout naming, so reuse it instead of duplicating the hash.
                from .feeds import _clone_dir
                directory = _clone_dir(ctx, module.source)
                if not (directory / '.git').is_dir():
                    continue
                module.path = directory
            else:
                raise Error(f'{kind} {name} has no source yet; run "unixnu fetch"')
        if kind == 'package':
            if name == 'main':
                _link(module.path, tree / 'package')
            else:
                _link(module.path, tree / 'kits' / kit)
        else:
            _link(module.path, tree / {'driver': 'drivers', 'kit': 'kits', 'patch': 'patches', 'refer': 'refer'}[kind] / name)
    if not (tree / 'package').is_symlink():
        raise Error('no package module: this repository or --local-package must provide package/')
    (tree / 'scripts').mkdir(exist_ok=True)
    for base in (ctx.root, ctx.device):
        if (base / 'scripts').is_dir():
            _copy_scripts(base / 'scripts', tree / 'scripts')
    return tree
