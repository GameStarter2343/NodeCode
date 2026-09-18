# Licensed under the General Public License 3.0

import lzma
import zstandard as zstd

def compress_lzma(payload: bytes, preset: int=6):
    '''Compress payload string using LZMA'''
    data = lzma.compress(payload.encode('utf-8'), preset=preset)
    print(data)
    return data

def decompress_lzma(payload: bytes):
    '''Compress payload string using LZMA'''
    data = lzma.decompress(payload)
    print(data)
    return data

def compress_zstd(payload: str, preset: int=19):
    '''Compress payload string using Zstd'''
    with open("zstd.dict", "rb") as f:
        dict = zstd.ZstdCompressionDict(f.read())
    compressor = zstd.ZstdCompressor(level=preset, dict_data=dict)
    return compressor.compress(payload.encode('utf-8'))

def decompress_zstd(payload: bytes):
    '''Decompress payload string using Zstd'''
    with open("zstd.dict", "rb") as f:
        dict = zstd.ZstdCompressionDict(f.read())
    decompressor = zstd.ZstdDecompressor(dict_data=dict)
    return decompressor.decompress(payload)