"""XNU kernel: build from the patched Apple source, then prelink against the original kernelcache."""
import os
from pathlib import Path

from . import ipsw
from .util import Error, expand, log, run, sha256_file, tool


def xnu_env(ctx):
    """CONFIG_XNU_* of the defconfig become XNU_* variables of the legacy kernel build script."""
    env = {}
    for key, value in ctx.need_config().items():
        if not key.startswith('XNU_'):
            continue
        if isinstance(value, str):
            value = value.replace('{tree}', str(ctx.tree))
        env[key] = '1' if value is True else '0' if value is False else str(value)
    return env


def version_env(modules):
    """Source versions of the Apple references, from feeds.toml, for the kernel scripts."""
    env = {}
    for name, prefix in (('xnu', 'XNU'), ('libplatform', 'LIBPLATFORM'), ('libdispatch', 'LIBDISPATCH'), ('AvailabilityVersions', 'AVAILABILITYVERSIONS')):
        module = modules.get(('refer', name))
        entry = module.source if module and module.source else {}
        if entry.get('ref'):
            env[f'UNIXNU_{prefix}_TAG'] = entry['ref']
        if entry.get('commit'):
            env[f'UNIXNU_{prefix}_COMMIT'] = entry['commit']
    xnu = (modules.get(('refer', 'xnu')) or None) and modules[('refer', 'xnu')].source or {}
    for key, variable in (('darwin', 'UNIXNU_XNU_DARWIN'), ('version', 'UNIXNU_XNU_VERSION'),
                          ('driverkit_tag', 'UNIXNU_DRIVERKIT_TAG'), ('trustcache_tag', 'UNIXNU_TRUSTCACHE_TAG')):
        if xnu.get(key):
            env[variable] = xnu[key]
    return env


def build(ctx, modules):
    log('kernel: XNU')
    for name in ('git', 'make', 'clang', 'cmake', 'ninja', 'unifdef'):
        tool(name, 'brew install ' + name)
    tool('xcodebuild', 'the kernel step builds with the Xcode iphoneos SDK')
    script = ctx.tree / 'scripts/build-xnu-4k-baseline.sh'
    if not script.is_file():
        raise Error(f'{script} missing')
    # The defconfig is the recorded device configuration, but an explicit variable in the caller's
    # environment has to win: without that, a documented one-off switch such as
    # XNU_SOURCE_PTHREAD=0 for the first pass cannot be exercised at all.
    configured = {**version_env(modules), **xnu_env(ctx)}
    env = {**configured, **os.environ}
    env['JOBS'] = str(ctx.jobs)
    record = ctx.tree / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/osfmk/DEVELOPMENT/startup.o.json'
    if env.get('XNU_BOOT_PROGRESS') == '1' and not record.is_file():
        # The boot progress layer is generated from the compile record of startup.o, which only a
        # previous build leaves behind, so a first build from scratch runs once without it.
        log('kernel: bootstrap pass without boot progress')
        run(['bash', script], cwd=ctx.tree, env={**env, 'XNU_BOOT_PROGRESS': '0'}, log_file=ctx.logs / 'kernel-bootstrap.log')
    run(['bash', script], cwd=ctx.tree, env=env, log_file=ctx.logs / 'kernel.log')


def prelink(ctx):
    """Produce out/<device>/kernel/kernel.macho from the XNU build and the original kernelcache."""
    log('kernel: prelink')
    spec = ctx.board.get('kernel', {}).get('prelink')
    if not spec:
        raise Error('board.toml has no [kernel.prelink]')
    cache = ctx.option('kernelcache', 'UNIXNU_KERNELCACHE') or (expand(ctx.tree / spec['kernelcache']) if spec.get('kernelcache') else None)
    if not cache:
        # Not given: take it from the IPSW (--ipsw PATH_OR_URL, or the url of board.toml [ipsw]).
        cache = ipsw.kernelcache(ctx, ipsw.Ipsw(ctx))
    if not cache.is_file():
        raise Error(f'kernelcache not found: {cache}')
    wanted = spec.get('kernelcache_sha256')
    if wanted and sha256_file(cache) != wanted:
        raise Error(f'{cache} does not match the kernelcache this board was qualified against (sha256 {wanted})')
    kernel = ctx.tree / spec['kernel']
    if not kernel.is_file():
        raise Error(f'{kernel} not found; run "unixnu build kernel" first')
    output = ctx.work / 'kernel/kernel.macho'
    output.parent.mkdir(parents=True, exist_ok=True)
    command = ['python3', ctx.tree / spec['tool'], '--kernel', kernel, '--prelinked', cache,
               f'--slide={spec["slide"]}', '--output', output]
    for root in spec.get('roots', []):
        command += ['--root', root]
    if spec.get('early_security_init'):
        command += ['--early-security-init', spec['early_security_init']]
    command += spec.get('extra_args', [])
    run(command, cwd=ctx.tree, log_file=ctx.logs / 'prelink.log')
    log(f'kernel: {output} sha256 {sha256_file(output)}')
    return output
