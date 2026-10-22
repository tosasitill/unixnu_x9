"""Android boot image: gzip(BootShim + UEFI FD) with the device tree appended, plus an optional ramdisk.

Inputs are a Mu-Silicium checkout (BootShim sources, Resources/Configs/<device>.toml, mkbootimg.py),
the built firmware volume and a compiled device tree. The image is meant for "fastboot boot" only.
The result is verified by unpacking it again; the output is deterministic.
"""
import gzip
import hashlib
import json
import shutil
import struct
import tempfile
import zlib
from pathlib import Path

from .config import load_toml
from .util import Error, log, run, sha256_file, tool

DEFAULTS = {'header_version': 0, 'base': 0, 'kernel_offset': 0x8000, 'tags_offset': 0x1e00000, 'pagesize': 4096}
MKBOOTIMG_FLAGS = ('base', 'kernel_offset', 'tags_offset', 'pagesize', 'header_version', 'ramdisk_offset', 'second_offset', 'cmdline')
ANDROID_HEADER_PAGE = 4096
V3_HEADER_SIZE = 1580
V4_HEADER_SIZE = 1584
V4_SIGNATURE_LIMIT = 4096


def empty_cpio():
    """Return a valid, payload-free newc archive for LK variants that reject size zero."""
    name = b'TRAILER!!!\0'
    fields = (
        b'070701',
        b'00000000',  # inode
        b'00000000',  # mode
        b'00000000',  # uid
        b'00000000',  # gid
        b'00000001',  # nlink
        b'00000000',  # mtime
        b'00000000',  # filesize
        b'00000000',  # devmajor
        b'00000000',  # devminor
        b'00000000',  # rdevmajor
        b'00000000',  # rdevminor
        b'0000000b',  # namesize, including the NUL
        b'00000000',  # check
    )
    record = b''.join(fields) + name
    return record + b'\0' * ((-len(record)) % 4)


def align(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def compile_shim(uefi, base, size, requires_header, scratch):
    clang, objcopy, nm = tool('clang'), tool('llvm-objcopy', 'brew install llvm'), tool('llvm-nm', 'brew install llvm')
    source = uefi / 'BootShim/BootShim.S'
    if not source.is_file():
        raise Error(f'{source} missing: --uefi must be a Mu-Silicium checkout')
    obj = scratch / 'shim.o'
    run([clang, '--target=aarch64-none-elf', '-c', f'-DFD_BASE={base}', f'-DFD_SIZE={size}',
         f'-DREQUIRES_KERNEL_HEADER={int(requires_header)}', source, '-o', obj], capture=True)
    run([objcopy, '-O', 'binary', obj, scratch / 'shim.bin'], capture=True)
    shim = (scratch / 'shim.bin').read_bytes()
    symbols = run([nm, '-n', obj], capture=True)
    offset = next((int(line.split()[0], 16) for line in symbols.splitlines() if line.endswith(' _Payload')), None)
    if offset != len(shim):
        raise Error('a literal pool or padding separates the _Payload label from the appended firmware volume')
    if requires_header and (shim[56:60] != b'ARM\x64'
                            or struct.unpack_from('<QQQ', shim, 8) != (0, len(shim) + size, 0xa)):
        raise Error('the ARM64 image header does not describe the shim plus the firmware volume')
    return shim


def patch_dtb(dtb, nodes, scratch):
    """Add device-tree nodes the stock bootloader expects (board.toml [[bootimg.dtb_node]])."""
    if not nodes:
        return dtb
    fdtput, fdtget = tool('fdtput', 'brew install dtc'), tool('fdtget', 'brew install dtc')
    work = scratch / 'device.dtb'
    work.write_bytes(dtb)
    for node in nodes:
        run([fdtput, '-c', work, node['path']], capture=True)
        for prop in node.get('props', []):
            values = prop['value'] if isinstance(prop['value'], list) else [prop['value']]
            run([fdtput, '-t', prop.get('type', 's'), work, node['path'], prop['name'], *[str(v) for v in values]], capture=True)
            got = run([fdtget, '-t', prop.get('type', 's'), work, node['path'], prop['name']], capture=True).split()
            kind = prop.get('type', 's')
            if kind in ('x', 'i', 'u') and [int(v, 16 if kind == 'x' else 10) for v in got] != [int(str(v), 0) for v in values]:
                raise Error(f'dtb node {node["path"]} property {prop["name"]} did not round-trip')
    return work.read_bytes()


def settings(ctx, uefi):
    fw = ctx.board.get('firmware', {})
    config = load_toml(uefi / 'Resources/Configs' / f'{fw["config"]}.toml') if fw.get('config') else {}
    values = dict(DEFAULTS)
    values.update(config.get('boot_image', {}))
    values.update(ctx.board.get('bootimg', {}).get('image', {}))
    fd = config.get('uefi_fd', {})
    return values, fd.get('base'), fd.get('size'), config.get('boot_shim', {}).get('requires_kernel_header', True), config


def build(ctx, uefi, fd, dtb, ramdisk=None, output=None):
    log('bootimg')
    image, base, size, requires_header, config = settings(ctx, uefi)
    mkbootimg = uefi / 'Resources/Scripts/mkbootimg.py'
    if not mkbootimg.is_file():
        raise Error(f'{mkbootimg} missing: --uefi must be a Mu-Silicium checkout')
    for path, what in ((fd, 'firmware volume (--fd)'), (dtb, 'device tree (--dtb)')):
        if not path or not Path(path).is_file():
            raise Error(f'{what} not found: {path}')
    firmware = Path(fd).read_bytes()
    if base is None or size is None:
        raise Error('the Mu-Silicium device config has no [uefi_fd] base and size')
    if len(firmware) != size:
        raise Error(f'firmware volume is {len(firmware):#x} bytes, the device config says {size:#x}')
    out = Path(output) if output else ctx.work / 'boot/boot.img'
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='unixnu-bootimg-') as scratch_name:
        scratch = Path(scratch_name)
        shim = compile_shim(uefi, base, size, requires_header, scratch)
        tree = patch_dtb(Path(dtb).read_bytes(), ctx.board.get('bootimg', {}).get('dtb_node', []), scratch)
        if tree[:4] != b'\xd0\x0d\xfe\xed' or struct.unpack_from('>I', tree, 4)[0] != len(tree):
            raise Error('invalid compiled device tree')
        combined = shim + firmware
        if config.get('boot_image_kernel', {}).get('kernel_compression') == 'none':
            # Ship the kernel blob uncompressed. expdb shows LK finds the FDT in
            # the loaded blob (dtb_addr = kernel_load + zimage_size) and boots
            # the blob as-is; a gzip stream makes the jump land on compressed
            # bytes, which no shim can survive. Uncompressed, the blob IS the
            # shim, so the first instructions after LK's jump are ours and the
            # shim performs the FD relocation itself.
            kernel = combined + tree
            kernel_path = scratch / 'Image-dtb'
        elif config.get('boot_image_kernel', {}).get('kernel_compression') == 'lz4':
            # The unlz4 input loop runs over [load, load+zimage_size) and
            # exits only when the input is exhausted: any byte after the
            # frame inside zimage_size (a terminator, padding) is parsed as
            # the next block header and fails ('dest_len or chunksize >
            # INT_MAX' for DTB bytes, 'decodes failure' for a zero csize).
            # So the DTB must start EXACTLY at the frame end, and the frame
            # itself must be 16-byte aligned for the ufdt overlay at :679.
            # The compressed size of an 8 MiB blob is invariant under zero
            # padding (lz4 RLE), so shift the frame alignment with a short
            # non-zero tail (five 0xff bytes) plus matching zero slack inside
            # the decompressed padding until (8 + csize) % 16 == 0.
            import lz4.block as _lz4b
            # The raw stock LZ4 output is 42,101,248 bytes (0x02826a00),
            # while its MZ header declares image_size=0x028e0000. The latter
            # is the header's memory footprint, not the byte count produced
            # by LZ4 (the difference is 0xb9600 bytes). Preserve both stock
            # values independently; do not write the output length into the
            # +8 field, which is zero in the stock header.
            target = 42101248
            stock_image_size = 0x028e0000
            blocks = []
            off = 0
            while off < target:
                blocks.append((off, min(8 * 1024 * 1024, target - off)))
                off += 8 * 1024 * 1024
            # Alter a short tail inside a zero-filled full block (block 4)
            # to control frame alignment without changing the shim/FD bytes or
            # total inflated length. Compress unaffected blocks once.
            align_block = 4
            align_start, align_len = blocks[align_block]
            base = bytearray(combined + b'\x00' * (target - len(combined)))
            base[16:24] = struct.pack('<Q', stock_image_size)
            prefix_parts = [b'\x02!\x4c\x18']
            for boff, blen in blocks[:align_block]:
                blk = _lz4b.compress(base[boff:boff + blen], store_size=False, compression=0)
                prefix_parts.extend((struct.pack('<I', len(blk)), blk))
            suffix_parts = []
            for boff, blen in blocks[align_block + 1:]:
                blk = _lz4b.compress(base[boff:boff + blen], store_size=False, compression=0)
                suffix_parts.extend((struct.pack('<I', len(blk)), blk))
            prefix_len = sum(map(len, prefix_parts))
            suffix_len = sum(map(len, suffix_parts))
            frame = padded = None
            for tail_len in range(1, 513):
                # Deterministic non-zero tail bytes are in decompressed padding;
                # the UEFI payload ends well before this block.
                tail = bytes((i * 73 + 19) & 0xff for i in range(tail_len))
                cand = bytearray(base)
                cand[align_start + align_len - tail_len:align_start + align_len] = tail
                cand = bytes(cand)
                middle = _lz4b.compress(cand[align_start:align_start + align_len],
                                        store_size=False, compression=0)
                if (prefix_len + 4 + len(middle) + suffix_len) % 16 == 0:
                    frame = prefix_parts + [struct.pack('<I', len(middle)), middle] + suffix_parts
                    padded = cand
                    break
            if not frame:
                raise Error('no 16-byte-aligned lz4 frame found')
            kernel = b''.join(frame)
            # The DTB follows the frame immediately: LK computes
            # zimage_size = kernel_sz - dtb_size = the frame length, so the
            # unlz4 input ends at the frame end and the DTB starts there.
            kernel += tree
            kernel_path = scratch / 'Image.lz4-dtb'
        else:
            # LK computes the appended-DTB address as kernel_load + the gzip
            # stream length (boot_linux_fdt:624-625), then overlays the DTBO
            # onto it. The ufdt overlay requires the FDT blob to be 4-byte
            # aligned; a gzip length that is not a multiple of 4 lands the DTB
            # misaligned and LK asserts at mt_boot.c:679 with FDT_ERR_ALIGNMENT
            # (observed 2026-10-22 04:03:50). Pad the gzip stream to a 16-byte
            # boundary; gunzip ignores bytes after the stream end.
            gz = gzip.compress(combined, mtime=0)
            gz += b'\x00' * (-len(gz) % 16)
            kernel = gz + tree
            kernel_path = scratch / 'Image.gz-dtb'
        kernel_path.write_bytes(kernel)
        if ramdisk:
            ramdisk_path = Path(ramdisk)
            if not ramdisk_path.is_file():
                raise Error(f'ramdisk not found: {ramdisk_path}')
        else:
            ramdisk_path = scratch / 'ramdisk'
            ramdisk_path.write_bytes(b'')
        if config.get('boot_image', {}).get('require_nonempty_ramdisk') and ramdisk_path.stat().st_size == 0:
            # This board's LK rejects a zero ramdisk target before it jumps to the
            # kernel. Keep --ramdisk none payload-free, but give LK a valid empty
            # newc archive so the legacy header still carries a non-zero address.
            # Never modify an explicitly supplied (possibly empty) input file.
            ramdisk_path = scratch / 'empty-ramdisk.cpio'
            ramdisk_path.write_bytes(empty_cpio())
        command = ['python3', mkbootimg, '--kernel', kernel_path, '--ramdisk', ramdisk_path, '-o', out]
        for key in MKBOOTIMG_FLAGS:
            if key in image:
                command += [f'--{key}', str(image[key])]
        run(command, capture=True)
        report = verify(out, combined, tree, ramdisk_path, image)
        # An image that does not fit the partition it is meant for is not a usable artifact, and the
        # failure is otherwise only discovered at flash time. The limit comes from the device
        # configuration, so a board that records its partition size gets this check for free.
        limit = config.get('boot_image', {}).get('partition_bytes')
        if limit and out.stat().st_size > limit:
            raise Error(f'boot image is {out.stat().st_size} bytes, but the {config.get("boot_image", {}).get("partition", "boot")} '
                        f'partition holds {limit}; lower [payload] size_mib or drop the ramdisk')
    report.update(image_sha256=sha256_file(out), image_bytes=out.stat().st_size, fd_sha256=sha256_file(fd),
                  dtb_sha256=sha256_file(dtb), shim_bytes=len(shim), hardware_tested=False)
    (out.parent / 'boot.json').write_text(json.dumps(report, indent=2) + '\n')
    log(f'bootimg: {out} ({out.stat().st_size} bytes)')
    return out


def verify(path, combined, tree, ramdisk_path, image):
    """Unpack the image and compare every part with its input."""
    data = Path(path).read_bytes()
    if len(data) < ANDROID_HEADER_PAGE or data[:8] != b'ANDROID!':
        raise Error('invalid or truncated Android boot image')
    version = struct.unpack_from('<I', data, 40)[0]
    if version != image['header_version']:
        raise Error('boot image header version does not match the requested version')

    kernel_size, ramdisk_size = struct.unpack_from('<II', data, 8)
    if version >= 3:
        page = ANDROID_HEADER_PAGE
        header_size = struct.unpack_from('<I', data, 20)[0]
        expected_header_size = V4_HEADER_SIZE if version == 4 else V3_HEADER_SIZE
        if version not in (3, 4) or image.get('pagesize', page) != page or header_size != expected_header_size:
            raise Error('Android v3/v4 header size or fixed page size is invalid')
        kernel_offset = page
        ramdisk_offset = align(kernel_offset + kernel_size, page)
        signature_size = struct.unpack_from('<I', data, V3_HEADER_SIZE)[0] if version == 4 else 0
        if signature_size > V4_SIGNATURE_LIMIT:
            raise Error('Android v4 boot signature exceeds the format limit')
        signature_offset = align(ramdisk_offset + ramdisk_size, page)
        expected_size = signature_offset + signature_size
        if len(data) != expected_size:
            raise Error('Android v3/v4 payload or signature size does not match its header')
        if any(data[kernel_offset + kernel_size:ramdisk_offset]) or any(data[ramdisk_offset + ramdisk_size:signature_offset]):
            raise Error('Android v3/v4 page padding is not zero-filled')
        if data[-64:-60] == b'AVBf':
            raise Error('fastboot boot image unexpectedly contains a partition AVB footer')
        kernel_address = tags = None
    else:
        kernel_size, kernel_address, ramdisk_size, ramdisk_address, second_size, _, tags, page, _, _ = struct.unpack_from('<10I', data, 8)
        if second_size or page != image['pagesize']:
            raise Error('legacy boot image header does not match the requested parameters')
        if kernel_address != image['base'] + image['kernel_offset'] or tags != image['base'] + image['tags_offset']:
            raise Error('boot image load addresses do not match the requested offsets')
        expected_ramdisk_address = image['base'] + image.get('ramdisk_offset', 0x01000000) if ramdisk_size else 0
        if ramdisk_address != expected_ramdisk_address:
            raise Error('boot image ramdisk load address does not match its size and configured offset')
        kernel_offset = page
        ramdisk_offset = page * (1 + -(-kernel_size // page))
        signature_size = 0

    kernel = data[kernel_offset:kernel_offset + kernel_size]
    if kernel[:4] == b'\x02!\x4c\x18':
        # LZ4 legacy frame: expand every [csize][data] block and compare with
        # the shim+FD blob. No appended DTB (LK uses the vendor_boot dtb).
        import lz4.block as _lz4b
        o, expanded = 4, b''
        while o + 4 <= len(kernel) and len(expanded) < 42101248:
            (csize,) = struct.unpack_from('<I', kernel, o)
            if csize == 0:
                break
            o += 4
            expanded += _lz4b.decompress(kernel[o:o + csize], uncompressed_size=8 * 1024 * 1024)
            o += csize
        if o % 16:
            raise Error('lz4 frame end is not 16-byte aligned for the ufdt overlay')
        expected = combined[:16] + struct.pack('<Q', 0x028e0000) + combined[24:]
        if expanded[:len(expected)] != expected:
            raise Error('lz4 frame does not round-trip to the shim plus firmware volume and stock MZ header')
        if len(expanded) != 42101248:
            raise Error('lz4 frame does not inflate to the stock kernel size')
        if kernel[o:o + 4] != b'\xd0\x0d\xfe\xed' or len(kernel) - o != len(tree):
            raise Error('lz4 frame is not followed by the device tree')
    elif kernel[:len(combined)] == combined and kernel[len(combined):].lstrip(b'\x00') == tree:
        # Uncompressed blob: [shim+FD][DTB]. Verify the DTB address LK will
        # compute (blob + shim+FD length) is 16-byte aligned for the ufdt fixup.
        if (len(combined) % 16) != 0:
            raise Error('uncompressed kernel blob leaves the appended DTB misaligned for ufdt')
    else:
        unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
        if unpacker.decompress(kernel) != combined or not unpacker.eof:
            raise Error('gzip, firmware and appended device tree do not round-trip')
        # The gzip stream may carry alignment padding after it (kept so the
        # appended DTB sits at an address LK's ufdt overlay accepts); it is not
        # part of the DTB blob.
        trailing = unpacker.unused_data
        if trailing.lstrip(b'\x00') != tree:
            raise Error('gzip, firmware and appended device tree do not round-trip')
    if data[ramdisk_offset:ramdisk_offset + ramdisk_size] != Path(ramdisk_path).read_bytes():
        raise Error('ramdisk bytes differ')
    return {'kernel_sha256': hashlib.sha256(kernel).hexdigest(), 'ramdisk_bytes': ramdisk_size,
            'header_version': version,
            'kernel_address': hex(kernel_address) if kernel_address is not None else None,
            'ramdisk_address': hex(ramdisk_address) if version < 3 and ramdisk_address else None,
            'tags_address': hex(tags) if tags is not None else None,
            'boot_signature_bytes': signature_size, 'avb_footer_present': False}
