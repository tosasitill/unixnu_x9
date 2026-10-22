#!/usr/bin/env python3
"""Run the real DarwinBoot ADT parser against the generated X9 device tree.

The parser in DarwinBoot.c is the authority for what the loader accepts, so this compiles the
function itself, with the EDK II names it expects, and walks the ADT it will be handed.
"""
import importlib.util
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def extract_parser() -> str:
    source = (ROOT / 'drivers/DarwinBoot/DarwinBootPkg/Driver/DarwinBoot.c').read_text()
    start = source.index('STATIC\nBOOLEAN\nAdtNode (')
    end = source.index('\nSTATIC\nBOOLEAN\nValidTrustCaches', start)
    return source[start:end]


PRELUDE = '''#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <limits.h>
typedef uint8_t UINT8; typedef uint32_t UINT32; typedef uint64_t UINT64;
typedef size_t UINTN; typedef int BOOLEAN; typedef char CHAR8;
#define STATIC static
#define CONST const
#define TRUE 1
#define FALSE 0
#define MAX_UINTN SIZE_MAX
#define ALIGN_VALUE(a, b) (((a) + (b) - 1) & ~((b) - 1))
#define AsciiStrCmp strcmp
#define CompareMem memcmp
static const void *ScanMem8(const void *p, size_t n, int v) { return memchr(p, v, n); }
static UINT32 U32(const void *p) { UINT32 v; memcpy(&v, p, 4); return v; }
static UINT64 U64(const void *p) { UINT64 v; memcpy(&v, p, 8); return v; }
'''


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print('usage: check-x9-adt-darwinboot.py ADT', file=sys.stderr)
        return 2
    adt = Path(argv[1]).read_bytes()
    harness = ROOT / 'build/x9-adt-darwinboot'
    harness.mkdir(parents=True, exist_ok=True)
    source = harness / 'parse.c'
    binary = harness / 'parse'
    source.write_text(PRELUDE + '#include "WindowSize.h"\n' + extract_parser() + f'''
int main(void) {{
  static const unsigned char tree[] = {{
{",".join(str(b) for b in adt)}
  }};
  UINTN offset = 0;
  UINTN ramdisk = MAX_UINTN, trust = MAX_UINTN;
  UINT64 window = 0;
  BOOLEAN keepPacked = FALSE;
  if (!AdtNode(tree, sizeof(tree), &offset, 0, 3, &ramdisk, &trust, &window, &keepPacked)) {{
    return 1;
  }}
  if (offset != sizeof(tree)) {{ return 2; }}
  if (ramdisk == MAX_UINTN) {{ return 3; }}
  if (trust == MAX_UINTN) {{ return 4; }}
  return 0;
}}
''')
    subprocess.run(['cc', '-std=c11', '-O1', '-I', str(ROOT / 'drivers/DarwinBoot/DarwinBootPkg/Driver'),
                    '-o', str(binary), str(source)], check=True)
    result = subprocess.run([str(binary)])
    codes = {0: 'accepted: RAMDisk + TrustCache placeholders resolved',
             1: 'rejected: AdtNode returned FALSE',
             2: 'rejected: parser did not consume the whole tree',
             3: 'rejected: no /chosen/memory-map/RAMDisk placeholder',
             4: 'rejected: no /chosen/memory-map/TrustCache placeholder'}
    print(codes.get(result.returncode, f'rejected: harness exit {result.returncode}'))
    return 0 if result.returncode == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
