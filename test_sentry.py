#!/usr/bin/env python3
"""
Payment Sentry Test - Polls API and calculates reward distributions
"""
import requests
import json
import time
from datetime import datetime

# Configuration
API_URL = "https://api.aipowergrid.io/api/v2/workers"
POLL_INTERVAL = 30  # seconds between polls
WORK_WEIGHT = 0.90
UPTIME_WEIGHT = 0.10
BLOCK_REWARD = 10  # tokens per block
BLOCKS_PER_INTERVAL = 1  # blocks per poll interval
TOTAL_PAYOUT = BLOCK_REWARD * 0.5 * BLOCKS_PER_INTERVAL  # 50% to workers

def fetch_workers():
    try:
        resp = requests.get(API_URL, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"Error fetching workers: {e}")
        return []

def calculate_payouts(old_data, new_data):
    old_map = {w["id"]: w for w in old_data}
    changes = {}
    
    for w in new_data:
        wid = w["id"]
        if wid not in old_map:
            continue
        
        old_details = old_map[wid].get("kudos_details", {})
        new_details = w.get("kudos_details", {})
        
        gen_diff = (new_details.get("generated") or 0) - (old_details.get("generated") or 0)
        up_diff = (new_details.get("uptime") or 0) - (old_details.get("uptime") or 0)
        
        if gen_diff > 0 or up_diff > 0:
            score = (gen_diff * WORK_WEIGHT) + (up_diff * UPTIME_WEIGHT)
            changes[wid] = {
                "name": w["name"],
                "wallet": w.get("wallet_address"),
                "gen_diff": gen_diff,
                "up_diff": up_diff,
                "score": score
            }
    
    return changes

def main():
    print("=" * 60)
    print("PAYMENT SENTRY TEST")
    print(f"Polling: {API_URL}")
    print(f"Interval: {POLL_INTERVAL}s")
    print(f"Weights: {WORK_WEIGHT*100}% work, {UPTIME_WEIGHT*100}% uptime")
    print(f"Payout per interval: {TOTAL_PAYOUT} tokens")
    print("=" * 60)
    print()
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Initial snapshot...")
    old_data = fetch_workers()
    print(f"  Got {len(old_data)} workers")
    
    polls = 0
    max_polls = 5  # Run 5 poll cycles for testing
    
    while polls < max_polls:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Waiting {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)
        
        polls += 1
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Poll #{polls}...")
        new_data = fetch_workers()
        print(f"  Got {len(new_data)} workers")
        
        changes = calculate_payouts(old_data, new_data)
        
        if not changes:
            print("  No kudos changes detected")
        else:
            total_score = sum(c["score"] for c in changes.values())
            print(f"\n  KUDOS CHANGES DETECTED:")
            print("-" * 60)
            
            for wid, c in sorted(changes.items(), key=lambda x: x[1]["score"], reverse=True):
                share = c["score"] / total_score if total_score > 0 else 0
                payout = share * TOTAL_PAYOUT
                
                wallet_short = c["wallet"][:12] + "..." if c["wallet"] else "NO WALLET"
                print(f"  {c['name'][:30]:<30}")
                print(f"    gen: +{c['gen_diff']:<8} up: +{c['up_diff']:<8}")
                print(f"    wallet: {wallet_short}")
                print(f"    payout: {payout:.6f} tokens ({share*100:.2f}%)")
                print()
            
            print("-" * 60)
            print(f"  TOTAL DISTRIBUTED: {TOTAL_PAYOUT} tokens")
        
        old_data = new_data
    
    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    main()
