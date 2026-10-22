"""Small helpers shared by the build driver."""
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


class Error(Exception):
    """A user-facing failure; the command line prints it without a traceback."""


def log(message):
    print(f'== {message}', flush=True)


def expand(path):
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


EXTRA_BIN = ('/opt/homebrew/opt/llvm/bin', '/opt/homebrew/opt/lld/bin', '/usr/local/opt/llvm/bin', '/usr/local/opt/lld/bin')


def tool(name, hint=''):
    """Find a tool on PATH, then in the usual Homebrew LLVM locations (keg-only, not on PATH)."""
    found = shutil.which(name) or shutil.which(name, path=os.pathsep.join(EXTRA_BIN))
    if not found:
        raise Error(f'{name} not found on PATH' + (f' ({hint})' if hint else ''))
    return found


def run(command, *, cwd=None, env=None, log_file=None, capture=False):
    """Run a command, echoing it. With log_file the output goes there and the tail is shown on failure."""
    command = [str(part) for part in command]
    if not capture:
        print('+ ' + ' '.join(command), flush=True)
    if capture:
        result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
        if result.returncode:
            raise Error(f'{command[0]} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}')
        return result.stdout
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        with Path(log_file).open('w') as stream:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            tail = Path(log_file).read_text(errors='replace').splitlines()[-30:]
            print('\n'.join(tail), file=sys.stderr)
            raise Error(f'{command[0]} failed ({result.returncode}); full log: {log_file}')
        return ''
    result = subprocess.run(command, cwd=cwd, env=env)
    if result.returncode:
        raise Error(f'{command[0]} failed ({result.returncode})')
    return ''


def git(repo, *args, capture=True):
    return run(['git', '-C', repo, *args], capture=capture)


CLANGPDB_TOOLS = ('clang', 'lld-link', 'llvm-lib', 'llvm-rc')


def _tool_major_version(executable):
    try:
        result = subprocess.run([str(executable), '--version'], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise Error(f'cannot query {executable} version: {error}') from error
    output = result.stdout + result.stderr
    match = re.search(r'\bversion\s+(\d+)(?:\.\d+)*', output, re.IGNORECASE)
    if match is None:
        match = re.search(r'\bLLD\s+(\d+)(?:\.\d+)*', output, re.IGNORECASE)
    if match is None:
        raise Error(f'cannot parse tool version from {executable}: {output.splitlines()[0]}')
    return int(match.group(1))


def clang_bin(directory):
    """Create a CLANGPDB tool set from LLVM/LLD builds with matching majors.

    On macOS, Homebrew may put an older standalone lld first on PATH while
    clang and its dylibs come from a newer LLVM keg. Resolve a matched pair
    before falling back to PATH, and reject a mismatched fallback explicitly.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    pair = None
    for prefix in (Path('/opt/homebrew'), Path('/usr/local')):
        opt = prefix / 'opt'
        llvm_bins = [opt / 'llvm' / 'bin']
        llvm_bins.extend(sorted(opt.glob('llvm@*/bin')))
        for llvm_bin in llvm_bins:
            clang = llvm_bin / 'clang'
            if not clang.exists():
                continue
            try:
                major = _tool_major_version(clang)
            except Error:
                continue
            lld_bins = (opt / f'lld@{major}' / 'bin', opt / 'lld' / 'bin')
            for lld_bin in lld_bins:
                linker = lld_bin / 'lld-link'
                if linker.exists() and _tool_major_version(linker) == major:
                    pair = (llvm_bin, lld_bin)
                    break
            if pair is not None:
                break
        if pair is not None:
            break

    if pair is None:
        clang = Path(tool('clang', 'install a matching LLVM/LLD toolchain'))
        linker = Path(tool('lld-link', 'install a matching LLVM/LLD toolchain'))
        clang_major = _tool_major_version(clang)
        linker_major = _tool_major_version(linker)
        if clang_major != linker_major:
            raise Error(f'clang {clang_major} and lld-link {linker_major} are incompatible; install matching LLVM/LLD versions')
        llvm_bin, lld_bin = clang.parent, linker.parent
    else:
        llvm_bin, lld_bin = pair

    targets = {
        'clang': llvm_bin / 'clang',
        'lld-link': lld_bin / 'lld-link',
        'llvm-lib': llvm_bin / 'llvm-lib',
        'llvm-rc': llvm_bin / 'llvm-rc',
    }
    for name in CLANGPDB_TOOLS:
        link = directory / name
        target = targets[name]
        if not target.exists():
            target = Path(tool(name, 'install the matching LLVM/LLD toolchain'))
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
    return str(directory) + '/'
