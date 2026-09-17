"""Pure evidence validation outside the vendor. Never writes or awards credits."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
import hashlib
import struct

DIFF1_TARGET = 0xffff << 208


def compact_target(bits: int) -> int:
    exponent, mantissa = bits >> 24, bits & 0x7fffff
    if bits & 0x800000 or mantissa == 0:
        raise ValueError("negative or zero network target")
    target = (mantissa >> (8 * (3 - exponent)) if exponent <= 3
              else mantissa << (8 * (exponent - 3)))
    if not 0 < target < 2**256:
        raise ValueError("network target outside uint256")
    return target


def difficulty(target: int) -> Decimal:
    if not 0 < target < 2**256:
        raise ValueError("target outside uint256")
    with localcontext() as ctx:
        ctx.prec = 80
        return Decimal(DIFF1_TARGET) / Decimal(target)


@dataclass(frozen=True)
class BlockWorkEvidence:
    block_hash: str
    network_target: int
    assigned_target: int
    assigned_difficulty: Decimal
    network_difficulty: Decimal
    meets_assigned_target: bool


def verify_block_work(*, header_hex: str, expected_hash: str,
                      assigned_target_be_hex: str) -> BlockWorkEvidence:
    header = bytes.fromhex(header_hex)
    target_bytes = bytes.fromhex(assigned_target_be_hex)
    if len(header) != 80 or len(target_bytes) != 32:
        raise ValueError("expected 80-byte header and 32-byte big-endian target")
    actual_hash = hashlib.sha256(hashlib.sha256(header).digest()).digest()[::-1].hex()
    if actual_hash != expected_hash.lower():
        raise ValueError("header hash does not match authoritative block hash")
    network = compact_target(struct.unpack_from('<I', header, 72)[0])
    value = int(actual_hash, 16)
    if value > network:
        raise ValueError("header does not meet network target")
    assigned = int.from_bytes(target_bytes, 'big')
    return BlockWorkEvidence(actual_hash, network, assigned,
                             difficulty(assigned), difficulty(network), value <= assigned)
