"""Bounded single-frame streaming compression for owned installation archives."""

from contextlib import contextmanager
from importlib.util import find_spec
import sys

zstd = None
if sys.version_info >= (3, 14):
    if find_spec("compression.zstd") is not None and find_spec("_zstd") is not None:
        from compression import zstd
elif find_spec("backports") is not None and find_spec("backports.zstd") is not None:
    from backports import zstd

CHUNK = 1024 * 1024
MAGIC = bytes.fromhex("28b52ffd")
DECODE_WINDOW_LOG = 26


class InstallationCodecError(ValueError):
    """A selected archive codec is unavailable or its complete frame is invalid."""


def zstd_available():
    return zstd is not None


def compression_kind(value):
    if value not in ("none", "zstd") or not isinstance(value, str):
        raise InstallationCodecError(
            "Choose none or zstd for installation backup compression."
        )
    if value == "zstd" and not zstd_available():
        raise InstallationCodecError(
            "Zstandard compression is unavailable in this DUMB Python runtime; use an image with its pinned codec dependency."
        )
    return value


def estimated_stored_bytes(tar_bytes, compression):
    compression_kind(compression)
    if compression == "none":
        return tar_bytes
    # Reserve above the complete uncompressed tar estimate, allowing generous
    # frame/block expansion rather than assuming a useful compression ratio.
    return tar_bytes + (tar_bytes + 127) // 128 + CHUNK


def _write_all(handle, data):
    pending = memoryview(data)
    while pending:
        written = handle.write(pending)
        if not isinstance(written, int) or written <= 0 or written > len(pending):
            raise InstallationCodecError(
                "The compressed archive output did not accept a complete write."
            )
        pending = pending[written:]


class _ZstdWriter:
    def __init__(self, handle):
        self.handle = handle
        self.compressor = zstd.ZstdCompressor(
            options={
                zstd.CompressionParameter.compression_level: 3,
                zstd.CompressionParameter.nb_workers: 0,
                zstd.CompressionParameter.checksum_flag: 1,
                zstd.CompressionParameter.window_log: 23,
            }
        )

    def write(self, data):
        for offset in range(0, len(data), CHUNK):
            _write_all(
                self.handle, self.compressor.compress(data[offset : offset + CHUNK])
            )
        return len(data)

    def finish(self):
        _write_all(self.handle, self.compressor.flush())

    def flush(self):
        self.handle.flush()


@contextmanager
def encoded_output(handle, compression):
    compression_kind(compression)
    if compression == "none":
        yield handle
        return
    try:
        writer = _ZstdWriter(handle)
        yield writer
        writer.finish()
    except zstd.ZstdError:
        raise InstallationCodecError(
            "The Zstandard archive could not be completely encoded."
        ) from None


class _PrefixReader:
    def __init__(self, handle, prefix):
        self.handle, self.prefix = handle, prefix

    def read(self, amount):
        if not isinstance(amount, int) or amount < 0:
            raise InstallationCodecError(
                "Installation archive reads must have a bounded size."
            )
        amount = min(amount, CHUNK)
        head, self.prefix = self.prefix[:amount], self.prefix[amount:]
        if len(head) < amount:
            return head + self.handle.read(amount - len(head))
        return head


class _ZstdReader:
    def __init__(self, handle):
        self.handle = handle
        self.decoder = zstd.ZstdDecompressor(
            options={
                zstd.DecompressionParameter.window_log_max: DECODE_WINDOW_LOG,
            }
        )
        self.complete = False

    def read(self, amount):
        if not isinstance(amount, int) or amount < 0:
            raise InstallationCodecError(
                "Installation archive reads must have a bounded size."
            )
        amount = min(amount, CHUNK)
        output = bytearray()
        try:
            while len(output) < amount and not self.complete:
                if self.decoder.eof:
                    if self.decoder.unused_data or self.handle.read(1):
                        raise InstallationCodecError(
                            "Unexpected bytes or additional frames follow the Zstandard installation archive."
                        )
                    self.complete = True
                    break
                data = self.handle.read(CHUNK) if self.decoder.needs_input else b""
                if self.decoder.needs_input and not data:
                    raise InstallationCodecError(
                        "The Zstandard installation archive is truncated."
                    )
                output.extend(
                    self.decoder.decompress(data, max_length=amount - len(output))
                )
        except zstd.ZstdError:
            raise InstallationCodecError(
                "The Zstandard installation archive failed its frame, checksum or decoder window check."
            ) from None
        return bytes(output)


def decoded_input(handle):
    prefix = b""
    while len(prefix) < len(MAGIC):
        chunk = handle.read(len(MAGIC) - len(prefix))
        if not chunk:
            break
        prefix += chunk
    stream = _PrefixReader(handle, prefix)
    if prefix == MAGIC:
        compression_kind("zstd")
        return _ZstdReader(stream), "zstd"
    return stream, "none"
