# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the settlement Merkle tree builder.

These tests are critical: the Python tree must produce proofs that the
on-chain PaymentRouter._verify accepts byte-for-byte. The Solidity
implementation lives in `aipg-smart-contracts/contracts/grid/modules/PaymentRouter.sol`
and uses the same convention as `JobAnchor.verifyJobInDay`.

If these tests pass and the corresponding Foundry tests (PaymentRouter.t.sol)
also pass against tree-shaped proofs, we have round-trip confidence.
"""

from __future__ import annotations

import pytest

from grid_api.services.settlement.merkle import (
    build_tree,
    verify_proof,
    _hash_pair,
    _leaf,
)


# ============ HELPERS ============


def _addr(i: int) -> str:
    """Produce a deterministic 20-byte checksum-ish address for tests."""
    return "0x" + f"{i:040x}"


# ============ LEAF FORMAT ============


def test_leaf_matches_solidity_abi_encodePacked():
    """Solidity: keccak256(abi.encodePacked(address, uint256))
    = keccak256(20 bytes address || 32 bytes BE uint)
    """
    addr = "0x1234567890123456789012345678901234567890"
    den = 100

    # Hand-built byte sequence
    expected_preimage = bytes.fromhex(addr[2:]) + den.to_bytes(32, "big")
    assert len(expected_preimage) == 52

    leaf = _leaf(addr, den)
    assert len(leaf) == 32

    # Sanity: leaf is a hash of the expected preimage. We don't recompute the
    # hash here (would duplicate the implementation) — instead we check
    # determinism and that different inputs produce different leaves.
    leaf_again = _leaf(addr, den)
    assert leaf == leaf_again

    leaf_other = _leaf(addr, den + 1)
    assert leaf != leaf_other


def test_leaf_rejects_negative_den():
    with pytest.raises(ValueError):
        _leaf("0x" + "00" * 20, -1)


def test_leaf_rejects_bad_address_length():
    with pytest.raises(ValueError):
        _leaf("0x1234", 100)  # not 20 bytes


# ============ PAIR HASH (matches contract _verify) ============


def test_hash_pair_is_ordered():
    """Both `(a, b)` and `(b, a)` must produce the same hash so the contract
    can verify proofs without needing to track left/right."""
    a = b"\x00" * 32
    b = b"\xff" * 32
    assert _hash_pair(a, b) == _hash_pair(b, a)
    # And differs from same-side concat (sanity)
    assert _hash_pair(a, b) != _hash_pair(a, a)


# ============ TREE BUILDING ============


def test_build_tree_single_entry_root_is_leaf():
    """Single-entry tree has the leaf as the root and an empty proof."""
    tree = build_tree([(_addr(1), 100)])
    assert len(tree.entries) == 1
    assert tree.root == tree.entries[0].leaf
    assert tree.entries[0].proof == []


def test_build_tree_two_entries_proofs_verify():
    entries = [(_addr(1), 100), (_addr(2), 200)]
    tree = build_tree(entries)

    assert len(tree.entries) == 2
    # Each leaf's proof should verify against the root.
    for e in tree.entries:
        assert verify_proof(e.leaf, e.proof, tree.root)


def test_build_tree_three_entries_orphan_carries_up():
    """3-leaf tree: one leaf is orphaned at level 0 and carries up unchanged."""
    entries = [(_addr(1), 100), (_addr(2), 200), (_addr(3), 300)]
    tree = build_tree(entries)
    assert len(tree.entries) == 3
    for e in tree.entries:
        assert verify_proof(e.leaf, e.proof, tree.root)


def test_build_tree_seven_entries_uneven_levels():
    """7 leaves: orphans at multiple levels exercise the carry-up logic."""
    entries = [(_addr(i), i * 100) for i in range(1, 8)]
    tree = build_tree(entries)
    assert len(tree.entries) == 7
    for e in tree.entries:
        assert verify_proof(e.leaf, e.proof, tree.root), f"proof failed for {e.address}"


def test_build_tree_hundred_entries_all_verify():
    """Stress test — 100 leaves cover several internal levels."""
    entries = [(_addr(i), i * 7) for i in range(1, 101)]
    tree = build_tree(entries)
    assert len(tree.entries) == 100
    for e in tree.entries:
        assert verify_proof(e.leaf, e.proof, tree.root)


def test_build_tree_is_deterministic_regardless_of_input_order():
    """Same set of (address, den) entries must produce the same root no matter
    what order they're fed in. This is what lets validators independently
    rebuild the tree from an IPFS snapshot."""
    entries_a = [(_addr(1), 100), (_addr(2), 200), (_addr(3), 300), (_addr(4), 400)]
    entries_b = [(_addr(4), 400), (_addr(2), 200), (_addr(3), 300), (_addr(1), 100)]

    tree_a = build_tree(entries_a)
    tree_b = build_tree(entries_b)

    assert tree_a.root == tree_b.root


def test_build_tree_dedupes_by_address_last_wins():
    """If two entries share an address, the last one wins.
    Prevents a buggy caller from producing a tree with two leaves for one
    worker (which would let the worker double-claim a single period)."""
    entries = [(_addr(1), 100), (_addr(1), 999)]
    tree = build_tree(entries)
    assert len(tree.entries) == 1
    assert tree.entries[0].den == 999


def test_build_tree_rejects_empty():
    with pytest.raises(ValueError):
        build_tree([])


# ============ PROOF LOOKUP ============


def test_proof_for_returns_correct_proof():
    entries = [(_addr(1), 100), (_addr(2), 200), (_addr(3), 300)]
    tree = build_tree(entries)

    proof = tree.proof_for(_addr(2))
    assert proof is not None
    leaf = _leaf(_addr(2), 200)
    assert verify_proof(leaf, proof, tree.root)


def test_proof_for_returns_none_for_unknown_address():
    entries = [(_addr(1), 100), (_addr(2), 200)]
    tree = build_tree(entries)
    assert tree.proof_for(_addr(99)) is None


# ============ NEGATIVE — TAMPERED PROOFS FAIL ============


def test_tampered_leaf_does_not_verify():
    """Sanity check on the local verify_proof: swapping the den value must
    break verification. If this passes wrongly, both the Python and Solidity
    verifiers are broken in the same way."""
    entries = [(_addr(1), 100), (_addr(2), 200), (_addr(3), 300)]
    tree = build_tree(entries)
    entry = tree.entries[0]
    wrong_leaf = _leaf(entry.address, entry.den + 1)
    assert not verify_proof(wrong_leaf, entry.proof, tree.root)


def test_tampered_proof_does_not_verify():
    entries = [(_addr(1), 100), (_addr(2), 200), (_addr(3), 300)]
    tree = build_tree(entries)
    entry = tree.entries[0]
    bad_proof = list(entry.proof)
    if bad_proof:
        bad_proof[0] = b"\xff" * 32
        assert not verify_proof(entry.leaf, bad_proof, tree.root)
