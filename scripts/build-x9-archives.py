#!/usr/bin/env python3
"""Build the X9 device archives that the kernel link step requires.

`scripts/build-xnu-4k-baseline.sh` links these archives when the defconfig enables the
matching CONFIG_XNU_* switches:

    build/armdt-platform/libArmDTPlatform.a   CONFIG_XNU_ARM_DT_PLATFORM
    build/armcpu/libArmCpu.a                  CONFIG_XNU_ARMCPU
    build/native-ufs/libNativeUfs.a           CONFIG_XNU_NATIVE_UFS

The kernel makefile records the exact compiler invocation for every translation unit in
`<obj>/<component>/DEVELOPMENT/<name>.o.json`. This script reuses those recorded flags so a
device driver is compiled with the same target, sysroot, deployment version and macro
definitions as the kernel it is linked into. Nothing is guessed from a separate flag list.

Scope: this produces archives for a source bring-up build. It does not prove that the driver
binds, that the hardware responds, or that the kernel boots. The report it writes keeps those
claims false.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

# Runs from the repository (scripts/) and from an assembled build tree (out/<board>/tree/scripts/).
# scripts/ is copied into the tree, so __file__ always resolves into the repository checkout; the
# build products live in the tree instead. Prefer a default tree that actually holds a kernel build.
# scripts/ is copied into the assembled tree, so __file__ does not identify the tree. Locate it from
# the working directory first (the tree root holds build/xnu/ios-obj), then from out/state.json.
def find_tree() -> Path:
    if '--tree' in sys.argv:
        return Path(sys.argv[sys.argv.index('--tree') + 1]).resolve()
    for path in (Path.cwd(), Path.cwd() / 'tree', Path.cwd() / 'out/x9/tree'):
        if (path / 'build/xnu/ios-obj').is_dir():
            return path.resolve()
    # out/state.json records the device repository; the tree sits next to it under out/<board>/tree.
    for base in (Path.cwd(), *Path.cwd().parents):
        state = base / 'out/state.json'
        if state.is_file():
            for tree in sorted((base / 'out').glob('*/tree')):
                if (tree / 'build/xnu/ios-obj').is_dir():
                    return tree.resolve()
    return Path.cwd().resolve()


ROOT = find_tree()

# (archive directory, archive name, CONFIG switch, [(source, representative kernel object)])
DEVICE_ARCHIVES = (
    (
        'armdt-platform',
        'libArmDTPlatform.a',
        'XNU_ARM_DT_PLATFORM',
        [('drivers/ArmDTPlatform/ArmDTPlatform.cpp', 'iokit/DEVELOPMENT/IOService')],
    ),
    (
        'armcpu',
        'libArmCpu.a',
        'XNU_ARMCPU',
        [('drivers/ArmCpu/ArmCpu.cpp', 'iokit/DEVELOPMENT/IOService')],
    ),
    (
        'native-ufs',
        'libNativeUfs.a',
        'XNU_NATIVE_UFS',
        [('drivers/NativeUfs/NativeUfs.cpp', 'iokit/DEVELOPMENT/IOService')],
    ),
)

# NativeUfs publishes its root windows as IOBlockStorageDevice subclasses. That class belongs to
# IOStorageFamily, which is a closed-source kext: the open XNU tree ships only the DriverKit
# (user-space) declaration under iokit/IOKitUser, not the in-kernel C++ interface. The archive can
# therefore only be built when a real IOStorageFamily header set is available; there is no honest
# local stub, because the driver overrides methods whose exact signatures come from that family.
CLOSED_SOURCE_HEADERS = {
    'native-ufs': ('IOKit/storage/IOBlockStorageDevice.h',),
}

# Flags that describe the compilation unit itself rather than the target ABI. Reusing them would
# make the driver inherit a kernel object's input/output names.
# Options that take a separate argument: the value has to be consumed with the option, or a bare
# "-D" would be left behind and the following token would become a positional input.
VALUE_FLAGS = ('-D', '-U', '-I', '-include', '-isystem', '-Xclang', '-arch', '-target', '--target',
               '-x', '-F', '-L', '-fmodule-map-file')
# Options that describe this translation unit's own input and output and must not be inherited.
DROP_FLAGS = ('-o', '-c', '-MD', '-MF', '-MJ', '-MP', '-MT', '-MQ')


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def header_available(tree: Path, header: str) -> bool:
    """Report whether a header is reachable from the kernel's own include search.

    The device driver is compiled with the recorded kernel flags, so the same directories decide
    what the driver can see. Only the open tree and the installed kernel headers are consulted;
    the SDK does not ship the in-kernel IOKit families.
    """
    roots = [tree / '.cache/xnu-work', tree / 'build/xnu/ios-obj/EXPORT_HDRS',
             tree / '.cache/fakeroot/usr/local/include/kernel',
             tree / '.cache/fakeroot/System/Library/Frameworks/Kernel.framework/Versions/A/Headers',
             tree / '.cache/fakeroot/System/Library/Frameworks/Kernel.framework/Versions/A/PrivateHeaders']
    return any((root / header).is_file() for root in roots)


def objdir(tree: Path) -> Path:
    return tree / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837'


def load_command(tree: Path, representative: str) -> list[str]:
    """The recorded compiler argv of one kernel object, from clang's -MJ output.

    The kernel makefile names the record after the object: C sources produce <name>.o.json and C++
    sources produce <name>.cpo.json. The representative is given without its source suffix
    (iokit/DEVELOPMENT/IOService), and either record is accepted.
    """
    base = objdir(tree) / representative
    record = next((path for path in (base.with_suffix('.cpo.json'), base.with_suffix('.o.json'))
                   if path.is_file()), None)
    if record is None:
        raise SystemExit(
            f'missing compile record {record}\n'
            'run "unixnu build kernel" once so the kernel writes its -MJ records'
        )
    # clang appends one JSON object per compilation of the same object file, so a rebuilt unit
    # leaves several records behind. The last one is the invocation that produced the current .o.
    decoder = json.JSONDecoder()
    text = record.read_text()
    entries, index = [], 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            entry, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break
        entries.append(entry)
    if not entries:
        raise SystemExit(f'{record} holds no compile record')
    entry = entries[-1]
    command = entry.get('arguments') or shlex.split(entry.get('command', ''))
    if not command:
        raise SystemExit(f'{record} has neither arguments nor command')
    return command


def strip_unit_flags(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split a recorded argv into (compiler, target flags) without the unit's own inputs/outputs."""
    compiler, flags = argv[0], []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in DROP_FLAGS:
            index += 2          # drop the option and its value
            continue
        if token in VALUE_FLAGS:
            flags.extend(argv[index:index + 2])   # keep the option and its value together
            index += 2
            continue
        if token.endswith(('.c', '.cpp', '.cc', '.cxx', '.s', '.S', '.o', '.d', '.json')):
            index += 1          # the unit's own input or output
            continue
        flags.append(token)
        index += 1
    return [compiler], flags


def compile_object(tree: Path, source: Path, flags: list[str], compiler: str, output: Path,
                   unit_dir: Path) -> None:
    """Compile one device source with the kernel's own recorded flags.

    The recorded argv is a complete, successful kernel invocation. Rather than reconstructing the
    include search by hand, keep it verbatim and replace only the unit's input and output. Every
    relative path in it is resolved against the unit's object directory, which is the working
    directory the kernel used.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    command: list[str] = [compiler]
    index = 1
    dropped_input = False
    while index < len(flags):
        token = flags[index]
        if token in ('-o',):
            index += 2                       # drop the recorded output name
            continue
        if token == '-c':
            index += 1                       # re-added below, next to the new input
            continue
        if token == '-I' and index + 1 < len(flags) and flags[index + 1] == '.':
            command.extend(['-I', str(unit_dir)])
            index += 2
            continue
        if token == '-include' and index + 1 < len(flags):
            candidate = unit_dir / flags[index + 1]
            command.extend(['-include', str(candidate if candidate.is_file() else flags[index + 1])])
            index += 2
            continue
        if token == '-xc++' or token == '-xc':
            command.extend(['-x', token[2:]])
            index += 1
            continue
        if (not dropped_input) and token.endswith(('.c', '.cpp', '.cc', '.cxx')):
            dropped_input = True             # the recorded source is replaced by ours
            index += 1
            continue
        command.append(token)
        index += 1
    command.extend(['-c', str(source), '-o', str(output)])
    print('  ' + ' '.join(shlex.quote(part) for part in command))
    subprocess.run(command, cwd=unit_dir, check=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tree', type=Path, default=ROOT,
                        help='assembled build tree (default: out/x9/tree)')
    parser.add_argument('--only', action='append', metavar='NAME',
                        help='build only these archives (armdt-platform, armcpu, native-ufs)')
    parser.add_argument('--report', type=Path, help='report path (default: <tree>/build/x9-archives.json)')
    args = parser.parse_args(argv)

    tree = args.tree.resolve()
    if not (tree / 'build/xnu/ios-obj').is_dir():
        raise SystemExit(f'{tree} has no build/xnu/ios-obj; run "unixnu build kernel" first')
    report_path = args.report or tree / 'build/x9-archives.json'
    wanted = set(args.only or ())

    report = {
        'tree': str(tree),
        'object_directory': str(objdir(tree)),
        'archives': {},
        'hardware_tested': False,
        'driver_bind_proven': False,
        'kernel_boot_proven': False,
        'scope': 'source bring-up archive build; no runtime qualification',
    }

    for directory, archive_name, switch, units in DEVICE_ARCHIVES:
        if wanted and directory not in wanted:
            continue
        print(f'archive {directory}/{archive_name} ({switch})')
        skipped = False
        argv_record = load_command(tree, units[0][1])
        compilers, flags = strip_unit_flags(argv_record)
        compiler = compilers[0]
        objects = []
        for relative, representative in units:
            source = tree / relative
            if not source.is_file():
                raise SystemExit(f'missing device source {source}')
            # Each unit takes the flags of its own representative kernel object, so a C++ driver and a
            # C driver do not share a language mode.
            _, unit_flags = strip_unit_flags(load_command(tree, representative))
            missing = [header for header in CLOSED_SOURCE_HEADERS.get(directory, ())
                       if not header_available(tree, header)]
            if missing:
                print(f'skip {directory}: closed-source header(s) not present: {", ".join(missing)}')
                report[directory] = {'status': 'blocked', 'missing_headers': missing}
                skipped = True
                break
            output = tree / 'build' / directory / (source.stem + '.o')
            compile_object(tree, source, unit_flags, compiler, output,
                           (objdir(tree) / representative).parent)
            objects.append(output)
        if skipped:
            continue
        archive = tree / 'build' / directory / archive_name
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.unlink(missing_ok=True)
        subprocess.run(['libtool', '-static', '-o', str(archive), *map(str, objects)], check=True)
        report['archives'][directory] = {
            'archive': str(archive),
            'sha256': sha256_file(archive),
            'bytes': archive.stat().st_size,
            'objects': [{'source': str(tree / units[i][0]), 'sha256': sha256_file(objects[i])}
                        for i in range(len(objects))],
            'config_switch': switch,
            'flags_source': str(objdir(tree) / units[0][1]),
        }
        print(f'  -> {archive} ({archive.stat().st_size} bytes)')

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(f'report {report_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
