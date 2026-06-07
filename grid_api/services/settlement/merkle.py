# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Merkle tree builder for the settlement bot.

Builds a tree of [worker, den] entries that the on-chain `PaymentRouter._verify`
can validate. The hash convention must match the contract exactly:

  * Leaf:        keccak256(abi.encodePacked(address, uint256))
  * Inner node:  keccak256(min(left, right) || max(left, right))   (ordered)
  * Orphan:      odd node at a level carries up unchanged (no duplication)

This matches OpenZeppelin's "sortPairs: true" MerkleTree convention and the
existing `JobAnchor.verifyJobInDay` implementation.

Usage:

    from grid_api.services.settlement.merkle import build_tree

    tree = build_tree([
        ("0xAbC...", 12_847),
        ("0xDeF...", 9_412),
        ("0x123...", 21_004),
    ])

    print(tree.root.hex())             # commit this on-chain
    for addr, den, proof in tree.entries:
        # hand each worker their proof
        client.notify(addr, den, proof)

The contract verifier walks the proof bottom-up. Any single tree built by this
module produces proofs that any peer's tree of the same entries will also accept
(deterministic).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable

# eth_utils provides keccak256, but to keep this module dependency-light we use
# a tiny pure-Python keccak shim if eth_utils isn't available. In practice
# system-core already depends on web3.py which brings eth_utils transitively.
try:
    from eth_utils import keccak as _keccak
    from eth_utils import to_bytes, to_checksum_address
except ImportError:  # pragma: no cover - eth_utils is a runtime dep of web3.py
    from Crypto.Hash import keccak as _crypto_keccak

    def _keccak(data: bytes) -> bytes:
        h = _crypto_keccak.new(digest_bits=256)
        h.update(data)
        return h.digest()

    def to_bytes(hexstr: str) -> bytes:
        return bytes.fromhex(hexstr.removeprefix("0x"))

    def to_checksum_address(addr: str) -> str:
        # Naive lowercase if eth_utils not available — works fine for hashing,
        # just not display. Hashing only cares about the 20 raw bytes.
        return addr.lower()


@dataclass
class TreeEntry:
    address: str          # checksum-cased
    den: int              # raw integer
    leaf: bytes           # keccak256(address || uint256(den))
    proof: list[bytes] = field(default_factory=list)


@dataclass
class MerkleTree:
    root: bytes
    entries: list[TreeEntry]

    def proof_for(self, address: str) -> list[bytes] | None:
        """Return the Merkle proof for `address`, or None if not in tree."""
        target = to_checksum_address(address)
        for entry in self.entries:
            if to_checksum_address(entry.address) == target:
                return entry.proof
        return None


def _leaf(address: str, den: int) -> bytes:
    """Match the Solidity: keccak256(abi.encodePacked(address, uint256))."""
    if den < 0:
        raise ValueError(f"den must be non-negative, got {den}")
    addr_bytes = to_bytes(to_checksum_address(address))
    if len(addr_bytes) != 20:
        raise ValueError(f"address {address} is not 20 bytes")
    den_bytes = den.to_bytes(32, "big")
    return _keccak(addr_bytes + den_bytes)


def _hash_pair(a: bytes, b: bytes) -> bytes:
    """Match the Solidity verify: ordered concat then keccak256."""
    return _keccak(a + b) if a <= b else _keccak(b + a)


def build_tree(entries: Iterable[tuple[str, int]]) -> MerkleTree:
    """Build a Merkle tree from [(address, den), ...] entries.

    Entries are deduplicated by address (last value wins) and sorted by leaf
    hash for determinism. The same input set always produces the same root,
    regardless of input order.

    Returns a `MerkleTree` whose `.entries` carries each leaf's proof, so the
    bot can hand workers their proofs without rebuilding the tree.
    """
    # Deduplicate while preserving the last-write-wins for stability across
    # callers who might pass duplicates from concurrent DB writes.
    by_address: dict[str, int] = {}
    for addr, den in entries:
        by_address[to_checksum_address(addr)] = int(den)

    if not by_address:
        raise ValueError("cannot build tree from zero entries")

    tree_entries = [
        TreeEntry(address=addr, den=den, leaf=_leaf(addr, den))
        for addr, den in by_address.items()
    ]

    # Sort by leaf hash. Determinism matters: bot, validators, and end users
    # rebuilding from the same IPFS snapshot must all derive the same root.
    tree_entries.sort(key=lambda e: e.leaf)

    if len(tree_entries) == 1:
        # Single-entry tree: root is the leaf, proof is empty.
        return MerkleTree(root=tree_entries[0].leaf, entries=tree_entries)

    # Build the tree bottom-up. At each level we track:
    #   level_nodes:  the hashes at this level (left-to-right)
    #   index_of:     for each tree_entry, which slot at this level its
    #                 ancestor occupies. This lets us collect proofs.
    level_nodes: list[bytes] = [e.leaf for e in tree_entries]
    index_of: list[int] = list(range(len(tree_entries)))

    while len(level_nodes) > 1:
        next_level: list[bytes] = []
        # Pair sibling nodes; orphan carries up unchanged.
        for i in range(0, len(level_nodes), 2):
            if i + 1 < len(level_nodes):
                left, right = level_nodes[i], level_nodes[i + 1]
                # Record the sibling for every leaf whose ancestor is at i or i+1.
                for ei, slot in enumerate(index_of):
                    if slot == i:
                        tree_entries[ei].proof.append(right)
                    elif slot == i + 1:
                        tree_entries[ei].proof.append(left)
                next_level.append(_hash_pair(left, right))
            else:
                # Orphan: no sibling to add to anyone's proof; just carries up.
                next_level.append(level_nodes[i])

        # Reindex: each leaf's new slot is its old slot // 2.
        index_of = [slot // 2 for slot in index_of]
        level_nodes = next_level

    return MerkleTree(root=level_nodes[0], entries=tree_entries)


def verify_proof(leaf: bytes, proof: list[bytes], root: bytes) -> bool:
    """Reference Python implementation matching the contract's _verify.

    Useful for testing the bot's tree output before submitting on-chain.
    """
    computed = leaf
    for sibling in proof:
        computed = _hash_pair(computed, sibling)
    return computed == root
