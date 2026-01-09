# SPDX-FileCopyrightText: 2026 AI Power Grid
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os

from eth_account import Account
from web3 import Web3

RPC_URL = "https://sepolia.base.org"
CONTRACT_ADDRESS = "0xe660455D4A83bbbbcfDCF4219ad82447a831c8A1"

# New wallet to grant REGISTRAR_ROLE
NEW_REGISTRAR = "0xe2dddddf4dd22e98265bbf0e6bdc1cb3a4bb26a8"

# REGISTRAR_ROLE hash
REGISTRAR_ROLE = Web3.keccak(text="REGISTRAR_ROLE")

ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "role", "type": "bytes32"},
            {"internalType": "address", "name": "account", "type": "address"},
        ],
        "name": "grantRole",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "role", "type": "bytes32"},
            {"internalType": "address", "name": "account", "type": "address"},
        ],
        "name": "hasRole",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Load private key
pk = os.environ.get("DEPLOYER_PK")
if not pk:
    pk = input("Enter admin private key (0x...): ").strip()

account = Account.from_key(pk)
contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=ABI)

print(f"Admin wallet: {account.address}")
print(f"Granting REGISTRAR_ROLE to: {NEW_REGISTRAR}")

# Check if already has role
has_role = contract.functions.hasRole(REGISTRAR_ROLE, Web3.to_checksum_address(NEW_REGISTRAR)).call()
if has_role:
    print("Wallet already has REGISTRAR_ROLE!")
else:
    tx = contract.functions.grantRole(REGISTRAR_ROLE, Web3.to_checksum_address(NEW_REGISTRAR)).build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
        },
    )

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"TX sent: {tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    print(f"Status: {'SUCCESS' if receipt['status'] == 1 else 'FAILED'}")
    print(f"Explorer: https://sepolia.basescan.org/tx/{tx_hash.hex()}")
