# AIPG Blockchain Integration Guide

## Overview

This guide documents how to deploy and configure on-chain model validation for AIPG Core on Base (Coinbase L2).

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  Job Request    │────▶│   AIPG Core      │────▶│  ModelRegistry      │
│  (flux.1-krea)  │     │   (Python)       │     │  (Base Sepolia/     │
└─────────────────┘     └──────────────────┘     │   Mainnet)          │
                               │                 └─────────────────────┘
                               │                          │
                               ▼                          ▼
                        ┌──────────────────┐     ┌─────────────────────┐
                        │  Worker          │     │  On-chain model     │
                        │  Assignment      │◀────│  validation         │
                        └──────────────────┘     └─────────────────────┘
```

## Deployed Contracts

### Base Sepolia (Testnet)
| Contract | Address |
|----------|---------|
| ModelRegistry | `0xe660455D4A83bbbbcfDCF4219ad82447a831c8A1` |
| RecipeVault | `0x26FAd52658A726927De3331C5F5D01a5b09aC685` |
| GridNFT | `0xa87Eb64534086e914A4437ac75a1b554A10C9934` |

### Base Mainnet (Production)
| Contract | Address |
|----------|---------|
| ModelRegistry | `TBD - Deploy using same process` |
| RecipeVault | `TBD` |
| GridNFT | `TBD` |

---

## Setup Instructions

### 1. Environment Configuration

Add to `.env`:

```bash
# Blockchain Integration
BLOCKCHAIN_ENABLED=true
MODEL_REGISTRY_ADDRESS=0xe660455D4A83bbbbcfDCF4219ad82447a831c8A1
BASE_RPC_URL=https://sepolia.base.org

# For mainnet:
# MODEL_REGISTRY_ADDRESS=0x<mainnet_address>
# BASE_RPC_URL=https://mainnet.base.org
```

### 2. Restart Services

```bash
bash restart_horde.sh
```

### 3. Verify Integration

Check logs for blockchain validation:
```bash
tail -f horde.log | grep -i "blockchain\|validation"
```

Expected output when job comes in:
```
Blockchain validation passed for WP <id> with models: ['flux.1-krea-dev']
```

---

## Registering Models On-Chain

### Prerequisites

1. **Admin wallet** with REGISTRAR_ROLE on ModelRegistry
2. **ETH** for gas (Base Sepolia or Mainnet)

### Wallet Addresses

| Wallet | Address | Purpose |
|--------|---------|---------|
| Admin | `0x65F40903c35C2fCa7153905b8C132B4dC2a97795` | Has DEFAULT_ADMIN_ROLE |
| Worker | `0xD6e36faB7b19aBc5789c41D1cD6600948c32f526` | Has REGISTRAR_ROLE |

### Grant REGISTRAR_ROLE (One-time setup)

```python
from web3 import Web3, Account

RPC_URL = "https://sepolia.base.org"  # or mainnet.base.org
CHAIN_ID = 84532  # or 8453 for mainnet
MODEL_REGISTRY = "0xe660455D4A83bbbbcfDCF4219ad82447a831c8A1"
ADMIN_PK = "0x..."  # Admin private key

w3 = Web3(Web3.HTTPProvider(RPC_URL))
admin = Account.from_key(ADMIN_PK)

ROLE_ABI = [
    {"inputs": [{"type": "bytes32", "name": "role"}, {"type": "address", "name": "account"}], 
     "name": "grantRole", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

contract = w3.eth.contract(address=Web3.to_checksum_address(MODEL_REGISTRY), abi=ROLE_ABI)

# REGISTRAR_ROLE hash
REGISTRAR_ROLE = bytes.fromhex("edcc084d3dcd65a1f7f23c65c46722faca6953d28e43150a467cf43e5c309238")
NEW_REGISTRAR = "0x..."  # Address to grant role to

tx = contract.functions.grantRole(REGISTRAR_ROLE, NEW_REGISTRAR).build_transaction({
    'from': admin.address,
    'nonce': w3.eth.get_transaction_count(admin.address),
    'gas': 100000,
    'gasPrice': w3.eth.gas_price,
    'chainId': CHAIN_ID
})

signed_tx = admin.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
print(f"TX: {tx_hash.hex()}")
```

### Register a Model

```python
from web3 import Web3, Account

RPC_URL = "https://sepolia.base.org"
CHAIN_ID = 84532
MODEL_REGISTRY = "0xe660455D4A83bbbbcfDCF4219ad82447a831c8A1"
REGISTRAR_PK = "0x..."  # Wallet with REGISTRAR_ROLE

w3 = Web3(Web3.HTTPProvider(RPC_URL))
acct = Account.from_key(REGISTRAR_PK)

MODEL_REGISTRY_ABI = [
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
            {"type": "string", "name": "architecture"}
        ],
        "name": "registerModel",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
]

contract = w3.eth.contract(address=Web3.to_checksum_address(MODEL_REGISTRY), abi=MODEL_REGISTRY_ABI)

# Model data
model = {
    "fileName": "flux1-krea-dev_fp8_scaled.safetensors",
    "displayName": "flux.1-krea-dev",
    "description": "FLUX.1-Krea-dev high quality image generation",
    "isNSFW": False,
    "sizeBytes": 17206700956,
    "modelType": 1,  # 0=TEXT, 1=IMAGE, 2=VIDEO
    "inpainting": False,
    "img2img": True,
    "controlnet": False,
    "lora": True,
    "baseModel": "flux_1",
    "architecture": "DiT"
}

model_hash = Web3.keccak(text=model["fileName"])

tx = contract.functions.registerModel(
    model_hash,
    model["modelType"],
    model["fileName"],
    model["displayName"],
    model["description"],
    model["isNSFW"],
    model["sizeBytes"],
    model["inpainting"],
    model["img2img"],
    model["controlnet"],
    model["lora"],
    model["baseModel"],
    model["architecture"]
).build_transaction({
    'from': acct.address,
    'nonce': w3.eth.get_transaction_count(acct.address),
    'gas': 500000,
    'gasPrice': w3.eth.gas_price,
    'chainId': CHAIN_ID
})

signed_tx = acct.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
print(f"Model registered! TX: {tx_hash.hex()}")
```

### Verify Model Registration

```python
model_hash = Web3.keccak(text="flux1-krea-dev_fp8_scaled.safetensors")
exists = contract.functions.isModelExists(model_hash).call()
print(f"Model exists: {exists}")
```

---

## Currently Registered Models

### Base Sepolia

| Model Name | File Name | Hash | Status |
|------------|-----------|------|--------|
| flux.1-krea-dev | flux1-krea-dev_fp8_scaled.safetensors | `0x90a3628...` | ✅ Registered |

### Models to Register

From usage stats (most popular):

| Model | Generations | Priority |
|-------|-------------|----------|
| Juggernaut XL | 1784 | High |
| Flux.1-Schnell fp8 | 1739 | High |
| stable_diffusion | 876 | High |
| SDXL 1.0 | 266 | Medium |
| DreamShaper XL | 175 | Medium |

---

## Mainnet Deployment Checklist

### 1. Deploy Contracts

```bash
# Using Hardhat or Foundry
npx hardhat run scripts/deploy.js --network base
```

### 2. Update Environment

```bash
# .env
BLOCKCHAIN_ENABLED=true
MODEL_REGISTRY_ADDRESS=0x<new_mainnet_address>
BASE_RPC_URL=https://mainnet.base.org
```

### 3. Grant Roles

Grant REGISTRAR_ROLE to the backend wallet.

### 4. Register Models

Register all production models using the script above.

### 5. Restart Services

```bash
bash restart_horde.sh
```

### 6. Verify

```bash
tail -f horde.log | grep -i "blockchain\|validation"
```

---

## Troubleshooting

### "Blockchain validation failed"

1. Check if model is registered:
```python
exists = contract.functions.isModelExists(model_hash).call()
```

2. Check the model filename matches exactly (case-sensitive)

3. Verify BLOCKCHAIN_ENABLED=true in .env

### "web3 package not installed"

```bash
pip install web3
```

### Connection errors

Check RPC_URL is correct:
- Sepolia: `https://sepolia.base.org`
- Mainnet: `https://mainnet.base.org`

---

## Files Modified

| File | Purpose |
|------|---------|
| `horde/blockchain/__init__.py` | Module init |
| `horde/blockchain/config.py` | Blockchain configuration |
| `horde/blockchain/model_registry.py` | ModelRegistry SDK |
| `horde/apis/v2/stable.py` | Job validation integration |
| `.env` | Environment configuration |

---

## Gas Costs (Approximate)

| Operation | Gas | Cost (at 0.001 gwei) |
|-----------|-----|----------------------|
| Grant Role | ~51,000 | ~0.00005 ETH |
| Register Model | ~380,000 | ~0.00038 ETH |
| Check Model Exists | 0 (view) | Free |

---

## Security Notes

1. **Never commit private keys** to git
2. Use environment variables for sensitive data
3. Admin wallet should be a multisig for production
4. REGISTRAR_ROLE should only be granted to trusted addresses

---

## Contact

For questions about blockchain integration, contact the AIPG team.

