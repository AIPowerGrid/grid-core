#!/usr/bin/env python3
"""
Sentry Test - Run this to watch for kudos changes
"""
import requests
import time
from datetime import datetime

API_URL = "https://api.aipowergrid.io/api/v2/workers"
WORK_WEIGHT = 0.90
UPTIME_WEIGHT = 0.10
TOTAL_PAYOUT = 5.0  # tokens per interval

print("=" * 60)
print("SENTRY TEST - Watching for kudos changes")
print(f"API: {API_URL}")
print("Press Ctrl+C to stop")
print("=" * 60)

old_data = requests.get(API_URL).json()
old_map = {w["id"]: w for w in old_data}
print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Initial: {len(old_data)} workers")

poll_count = 0
while True:
    time.sleep(60)  # Poll every 60 seconds
    poll_count += 1
    
    try:
        new_data = requests.get(API_URL).json()
    except:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] API error, retrying...")
        continue
    
    changes = []
    for w in new_data:
        wid = w["id"]
        if wid not in old_map:
            continue
        
        old = old_map[wid].get("kudos_details", {})
        new = w.get("kudos_details", {})
        
        gen_diff = (new.get("generated") or 0) - (old.get("generated") or 0)
        up_diff = (new.get("uptime") or 0) - (old.get("uptime") or 0)
        
        if gen_diff > 0 or up_diff > 0:
            score = (gen_diff * WORK_WEIGHT) + (up_diff * UPTIME_WEIGHT)
            changes.append({
                "name": w["name"],
                "wallet": w.get("wallet_address"),
                "gen": gen_diff,
                "up": up_diff,
                "score": score
            })
    
    if changes:
        total_score = sum(c["score"] for c in changes)
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] CHANGES DETECTED!")
        print("-" * 50)
        for c in sorted(changes, key=lambda x: x["score"], reverse=True):
            share = c["score"] / total_score if total_score > 0 else 0
            payout = share * TOTAL_PAYOUT
            print(f"  {c['name'][:35]}")
            print(f"    +{c['gen']} gen, +{c['up']} up -> {payout:.4f} tokens")
            print(f"    wallet: {c['wallet']}")
        print("-" * 50)
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Poll #{poll_count}: No changes ({len(new_data)} workers)")
    
    old_map = {w["id"]: w for w in new_data}
