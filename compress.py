# Licensed under the General Public License 3.0

import lzma
import zstandard as zstd

zstd_dict = None

def compress_lzma(payload: str, preset: int = 6) -> bytes:
    '''Compress payload string using LZMA'''
    return lzma.compress(payload.encode('utf-8'), preset=preset)
def decompress_lzma(payload: bytes) -> str:
    '''Decompress LZMA bytes back into a string'''
    return lzma.decompress(payload).decode('utf-8')

def load_zstd_dict(dict_path: str = "zstd.dict") -> zstd.ZstdCompressionDict:
    with open(dict_path, "rb") as f:
        global zstd_dict
        zstd_dict = zstd.ZstdCompressionDict(f.read())

def compress_zstd(payload: str, preset: int = 19) -> bytes:
    '''Compress payload string using Zstd'''
    compressor = zstd.ZstdCompressor(level=preset, dict_data=zstd_dict)
    return compressor.compress(payload.encode('utf-8'))

def decompress_zstd(payload: bytes) -> str:
    '''Decompress Zstd bytes back into a string'''
    decompressor = zstd.ZstdDecompressor(dict_data=zstd_dict)
    return decompressor.decompress(payload).decode('utf-8')