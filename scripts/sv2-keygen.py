#!/usr/bin/env python3
"""
SV2 Authority Key Generator - Pure Python (stdlib only)

Usage:
    ./sv2-keygen.py                    → Generate new random keypair
    ./sv2-keygen.py <secret_key>       → Derive public key from secret key
"""

import os
import hashlib
import sys

# ========================== BASE58 ==========================
BASE58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

def base58_encode(data: bytes) -> str:
    n = int.from_bytes(data, 'big')
    encoded = ''
    while n:
        n, rem = divmod(n, 58)
        encoded = BASE58_ALPHABET[rem] + encoded
    leading = len(data) - len(data.lstrip(b'\0'))
    return '1' * leading + encoded


def base58_decode(text: str) -> bytes:
    n = 0
    for char in text:
        n = n * 58 + BASE58_ALPHABET.index(char)
    leading = len(text) - len(text.lstrip('1'))
    return b'\0' * leading + n.to_bytes((n.bit_length() + 7) // 8, 'big')


def base58check_encode(payload: bytes) -> str:
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return base58_encode(payload + checksum)


def base58check_decode(text: str) -> bytes:
    data = base58_decode(text)
    if len(data) < 4:
        raise ValueError("Too short")
    payload, checksum = data[:-4], data[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        raise ValueError("Invalid checksum")
    return payload

# ========================== SECP256K1 ==========================
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

class Point:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y

    def __add__(self, other):
        if self.x == 0 and self.y == 0: return other
        if other.x == 0 and other.y == 0: return self
        if self.x == other.x and self.y == other.y:
            lam = (3 * self.x * self.x * pow(2 * self.y, -1, P)) % P
        else:
            lam = ((other.y - self.y) * pow(other.x - self.x, -1, P)) % P
        x3 = (lam * lam - self.x - other.x) % P
        y3 = (lam * (self.x - x3) - self.y) % P
        return Point(x3, y3)

    def __mul__(self, scalar: int):
        result = Point(0, 0)
        addend = self
        while scalar:
            if scalar & 1:
                result = result + addend if (result.x or result.y) else addend
            addend = addend + addend
            scalar >>= 1
        return result

G = Point(GX, GY)

def priv_to_pub(priv_bytes: bytes) -> bytes:
    priv = int.from_bytes(priv_bytes, 'big')
    if not (1 <= priv < N):
        raise ValueError("Private key out of range")
    return (G * priv).x.to_bytes(32, 'big')

# ========================== MAIN ==========================
def main():
    if len(sys.argv) > 1:
        # === Derive from secret key ===
        secret_str = sys.argv[1].strip().replace('"', '')
        try:
            priv_bytes = base58check_decode(secret_str)
            if len(priv_bytes) != 32:
                raise ValueError("Must be 32 bytes")
        except Exception as e:
            print(f"Error: Invalid secret key → {e}")
            sys.exit(1)

        pub_bytes = priv_to_pub(priv_bytes)
        pub_str = base58check_encode(b'\x01\x00' + pub_bytes)

        print(f'authority_public_key = "{pub_str}"')
        print(f'authority_secret_key = "{secret_str}"')

    else:
        # === Generate new keypair ===
        while True:
            priv_bytes = os.urandom(32)
            if 1 <= int.from_bytes(priv_bytes, 'big') < N:
                break

        pub_bytes = priv_to_pub(priv_bytes)
        secret_str = base58check_encode(priv_bytes)
        pub_str = base58check_encode(b'\x01\x00' + pub_bytes)

        print(f'authority_public_key = "{pub_str}"')
        print(f'authority_secret_key = "{secret_str}"')

if __name__ == "__main__":
    main()