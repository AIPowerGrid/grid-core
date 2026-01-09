#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 AI Power Grid
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Deploy ModelRegistry contract to Base Sepolia and register initial models.

Usage:
    python scripts/deploy_model_registry.py deploy
    python scripts/deploy_model_registry.py register <model_name>
    python scripts/deploy_model_registry.py list
"""

import sys

from web3 import Account, Web3

# Base Sepolia config
RPC_URL = "https://sepolia.base.org"
CHAIN_ID = 84532

# Contract bytecode would be compiled from Solidity
# For now, we'll use a simplified approach - check if contract exists first

# ModelRegistry ABI for write operations
MODEL_REGISTRY_ABI = [
    {"inputs": [], "name": "totalModels", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [{"type": "bytes32", "name": "modelHash"}],
        "name": "isModelExists",
        "outputs": [{"type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"type": "bytes32", "name": "modelHash"},
            {"type": "uint8", "name": "modelType"},
            {"type": "string", "name": "fileName"},
            {"type": "string", "name": "displayName"},
            {"type": "string", "name": "description"},
            {"type": "bool", "name": "isNSFW"},
            {"type": "uint256", "name": "sizeBytes"},
            {"type": "bool", "name": "inpainting"},
            {"type": "bool", "name": "img2img"},
            {"type": "bool", "name": "controlnet"},
            {"type": "bool", "name": "lora"},
            {"type": "string", "name": "baseModel"},
            {"type": "string", "name": "architecture"},
        ],
        "name": "registerModel",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"type": "string", "name": "modelId"},
            {"type": "uint16", "name": "stepsMin"},
            {"type": "uint16", "name": "stepsMax"},
            {"type": "uint16", "name": "cfgMinTenths"},
            {"type": "uint16", "name": "cfgMaxTenths"},
            {"type": "uint8", "name": "clipSkip"},
        ],
        "name": "setModelNumericConstraints",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"type": "string", "name": "modelId"}, {"type": "bytes32[]", "name": "samplerHashes"}],
        "name": "setModelAllowedSamplers",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"type": "string", "name": "modelId"}, {"type": "bytes32[]", "name": "schedulerHashes"}],
        "name": "setModelAllowedSchedulers",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"type": "string", "name": "modelId"}],
        "name": "getModelConstraints",
        "outputs": [
            {"type": "bool", "name": "exists"},
            {"type": "uint16", "name": "stepsMin"},
            {"type": "uint16", "name": "stepsMax"},
            {"type": "uint16", "name": "cfgMinTenths"},
            {"type": "uint16", "name": "cfgMaxTenths"},
            {"type": "uint8", "name": "clipSkip"},
            {"type": "bytes32[]", "name": "allowedSamplers"},
            {"type": "bytes32[]", "name": "allowedSchedulers"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"type": "uint256", "name": "modelId"}],
        "name": "getModel",
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"type": "bytes32", "name": "modelHash"},
                    {"type": "uint8", "name": "modelType"},
                    {"type": "string", "name": "fileName"},
                    {"type": "string", "name": "name"},
                    {"type": "string", "name": "description"},
                    {"type": "bool", "name": "isNSFW"},
                    {"type": "uint256", "name": "sizeBytes"},
                    {"type": "uint256", "name": "timestamp"},
                    {"type": "address", "name": "creator"},
                    {"type": "bool", "name": "inpainting"},
                    {"type": "bool", "name": "img2img"},
                    {"type": "bool", "name": "controlnet"},
                    {"type": "bool", "name": "lora"},
                    {"type": "string", "name": "baseModel"},
                    {"type": "string", "name": "architecture"},
                ],
            },
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

# Model definitions from stable_diffusion.json
MODELS = {
    "flux.1-krea-dev": {
        "fileName": "flux1-krea-dev_fp8_scaled.safetensors",
        "displayName": "flux.1-krea-dev",
        "description": "FLUX.1-Krea-dev vanilla (non-uncensored) variant",
        "isNSFW": False,
        "sizeBytes": 17206700956,
        "modelType": 1,  # IMAGE_MODEL
        "inpainting": False,
        "img2img": True,
        "controlnet": False,
        "lora": True,
        "baseModel": "flux_1",
        "architecture": "DiT",
        "constraints": {
            "stepsMin": 28,
            "stepsMax": 32,
            "cfgMinTenths": 35,  # 3.5 * 10
            "cfgMaxTenths": 50,  # 5.0 * 10
            "clipSkip": 1,
            "samplers": ["k_euler"],
            "schedulers": ["karras"],
        },
    },
    "Juggernaut XL": {
        "fileName": "juggernaut_xl.safetensors",
        "displayName": "Juggernaut XL",
        "description": "Very popular realistic SDXL model",
        "isNSFW": True,
        "sizeBytes": 6938816589,
        "modelType": 1,
        "inpainting": False,
        "img2img": True,
        "controlnet": False,
        "lora": True,
        "baseModel": "stable_diffusion_xl",
        "architecture": "UNet",
        "constraints": {
            "stepsMin": 20,
            "stepsMax": 50,
            "cfgMinTenths": 50,
            "cfgMaxTenths": 100,
            "clipSkip": 1,
            "samplers": ["k_euler", "k_dpmpp_2m", "k_dpmpp_sde"],
            "schedulers": ["karras", "simple"],
        },
    },
    "Flux.1-Schnell fp8 (Compact)": {
        "fileName": "flux1CompactCLIPAnd_Flux1SchnellFp8.safetensors",
        "displayName": "Flux.1-Schnell fp8 (Compact)",
        "description": "FLUX.1 [schnell] fast image generation model",
        "isNSFW": False,
        "sizeBytes": 17246524772,
        "modelType": 1,
        "inpainting": False,
        "img2img": True,
        "controlnet": False,
        "lora": True,
        "baseModel": "flux_1",
        "architecture": "DiT",
        "constraints": {
            "stepsMin": 3,
            "stepsMax": 8,
            "cfgMinTenths": 10,
            "cfgMaxTenths": 40,
            "clipSkip": 1,
            "samplers": ["k_euler"],
            "schedulers": ["karras"],
        },
    },
    "stable_diffusion": {
        "fileName": "model_1_5.ckpt",
        "displayName": "Stable Diffusion 1.5",
        "description": "The baseline generalist AI image generating model",
        "isNSFW": False,
        "sizeBytes": 4265380512,
        "modelType": 1,
        "inpainting": False,
        "img2img": True,
        "controlnet": True,
        "lora": True,
        "baseModel": "stable_diffusion_1",
        "architecture": "UNet",
        "constraints": {
            "stepsMin": 10,
            "stepsMax": 150,
            "cfgMinTenths": 10,
            "cfgMaxTenths": 300,
            "clipSkip": 1,
            "samplers": ["k_euler", "k_euler_a", "k_dpmpp_2m", "k_dpmpp_sde", "k_lms"],
            "schedulers": ["karras", "simple", "normal"],
        },
    },
    "SDXL 1.0": {
        "fileName": "sd_xl_base_1.0.safetensors",
        "displayName": "SDXL 1.0",
        "description": "The base SDXL 1.0 model",
        "isNSFW": False,
        "sizeBytes": 6938078334,
        "modelType": 1,
        "inpainting": False,
        "img2img": True,
        "controlnet": False,
        "lora": True,
        "baseModel": "stable_diffusion_xl",
        "architecture": "UNet",
        "constraints": {
            "stepsMin": 20,
            "stepsMax": 50,
            "cfgMinTenths": 50,
            "cfgMaxTenths": 100,
            "clipSkip": 1,
            "samplers": ["k_euler", "k_dpmpp_2m", "k_dpmpp_sde"],
            "schedulers": ["karras", "simple"],
        },
    },
}


def get_model_hash(file_name: str) -> bytes:
    """Generate a deterministic hash for a model based on filename."""
    return Web3.keccak(text=file_name)


def get_sampler_hash(sampler: str) -> bytes:
    """Generate hash for sampler name."""
    return Web3.keccak(text=sampler)


def check_balance(w3: Web3, address: str):
    """Check ETH balance."""
    balance = w3.eth.get_balance(address)
    return w3.from_wei(balance, "ether")


def register_model(w3: Web3, contract, account, model_name: str, model_data: dict):
    """Register a model on-chain."""
    print(f"\n📝 Registering model: {model_name}")

    model_hash = get_model_hash(model_data["fileName"])
    print(f"   Model hash: {model_hash.hex()}")

    # Check if already registered
    try:
        exists = contract.functions.isModelExists(model_hash).call()
        if exists:
            print("   ⚠️  Model already registered!")
            return None
    except Exception as e:
        print(f"   Note: Could not check existence: {e}")

    # Build transaction
    tx = contract.functions.registerModel(
        model_hash,
        model_data["modelType"],
        model_data["fileName"],
        model_data["displayName"],
        model_data["description"],
        model_data["isNSFW"],
        model_data["sizeBytes"],
        model_data["inpainting"],
        model_data["img2img"],
        model_data["controlnet"],
        model_data["lora"],
        model_data["baseModel"],
        model_data["architecture"],
    ).build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 500000,
            "gasPrice": w3.eth.gas_price,
            "chainId": CHAIN_ID,
        },
    )

    # Sign and send
    signed_tx = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    print(f"   📤 TX sent: {tx_hash.hex()}")

    # Wait for confirmation
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] == 1:
        print("   ✅ Model registered successfully!")
        return receipt
    else:
        print("   ❌ Transaction failed!")
        return None


def set_model_constraints(w3: Web3, contract, account, model_name: str, model_data: dict):
    """Set constraints for a registered model."""
    constraints = model_data.get("constraints", {})
    if not constraints:
        print(f"   No constraints defined for {model_name}")
        return

    print(f"\n⚙️  Setting constraints for: {model_name}")

    # Set numeric constraints
    tx = contract.functions.setModelNumericConstraints(
        model_data["fileName"],
        constraints["stepsMin"],
        constraints["stepsMax"],
        constraints["cfgMinTenths"],
        constraints["cfgMaxTenths"],
        constraints["clipSkip"],
    ).build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 200000,
            "gasPrice": w3.eth.gas_price,
            "chainId": CHAIN_ID,
        },
    )

    signed_tx = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    print(f"   📤 Numeric constraints TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] != 1:
        print("   ❌ Failed to set numeric constraints!")
        return

    # Set samplers
    if constraints.get("samplers"):
        sampler_hashes = [get_sampler_hash(s) for s in constraints["samplers"]]
        tx = contract.functions.setModelAllowedSamplers(model_data["fileName"], sampler_hashes).build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 200000,
                "gasPrice": w3.eth.gas_price,
                "chainId": CHAIN_ID,
            },
        )
        signed_tx = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"   📤 Samplers TX: {tx_hash.hex()}")
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    # Set schedulers
    if constraints.get("schedulers"):
        scheduler_hashes = [get_sampler_hash(s) for s in constraints["schedulers"]]
        tx = contract.functions.setModelAllowedSchedulers(model_data["fileName"], scheduler_hashes).build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 200000,
                "gasPrice": w3.eth.gas_price,
                "chainId": CHAIN_ID,
            },
        )
        signed_tx = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"   📤 Schedulers TX: {tx_hash.hex()}")
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    print("   ✅ Constraints set!")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python deploy_model_registry.py balance")
        print("  python deploy_model_registry.py register <model_name>")
        print("  python deploy_model_registry.py register-all")
        print("  python deploy_model_registry.py list")
        print("\nAvailable models:")
        for name in MODELS.keys():
            print(f"  - {name}")
        sys.exit(1)

    command = sys.argv[1]

    # Connect to Base Sepolia
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("❌ Failed to connect to Base Sepolia")
        sys.exit(1)
    print(f"✅ Connected to Base Sepolia (Chain ID: {CHAIN_ID})")

    # Load private key from environment or prompt
    import os

    pk = os.environ.get("DEPLOYER_PK")
    if not pk:
        pk = input("Enter private key (0x...): ").strip()

    account = Account.from_key(pk)
    print(f"📍 Wallet: {account.address}")

    balance = check_balance(w3, account.address)
    print(f"💰 Balance: {balance} ETH")

    if command == "balance":
        sys.exit(0)

    # Check for contract address
    contract_address = os.environ.get("MODEL_REGISTRY_ADDRESS")
    if not contract_address:
        print("\n⚠️  MODEL_REGISTRY_ADDRESS not set!")
        print("   You need to deploy the ModelRegistry contract first.")
        print("   Set the address: export MODEL_REGISTRY_ADDRESS=0x...")
        sys.exit(1)

    contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=MODEL_REGISTRY_ABI)
    print(f"📋 ModelRegistry: {contract_address}")

    if command == "list":
        try:
            total = contract.functions.totalModels().call()
            print(f"\n📊 Total registered models: {total}")
            for i in range(total):
                model = contract.functions.getModel(i).call()
                print(f"   {i}: {model[3]} ({model[2]})")
        except Exception as e:
            print(f"Error listing models: {e}")

    elif command == "register":
        if len(sys.argv) < 3:
            print("Usage: register <model_name>")
            sys.exit(1)

        model_name = sys.argv[2]
        if model_name not in MODELS:
            print(f"Unknown model: {model_name}")
            print("Available models:", list(MODELS.keys()))
            sys.exit(1)

        model_data = MODELS[model_name]
        receipt = register_model(w3, contract, account, model_name, model_data)
        if receipt:
            set_model_constraints(w3, contract, account, model_name, model_data)

    elif command == "register-all":
        for model_name, model_data in MODELS.items():
            receipt = register_model(w3, contract, account, model_name, model_data)
            if receipt:
                set_model_constraints(w3, contract, account, model_name, model_data)

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
