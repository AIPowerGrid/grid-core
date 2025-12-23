#!/usr/bin/env python3
"""
Simple test to debug model validation issues
- Downloads the model reference JSON directly (no horde imports)
- Rebuilds the stable_diffusion_names set like server does
- Simulates ImageWorker.parse_models() accept/reject logic
- Can simulate server failure (e.g., empty reference) to reproduce 400 BadRequest
"""

import os
import sys
import json
import requests
from typing import List, Set

# Defaults
REF_URL_DEFAULT = "https://raw.githubusercontent.com/AIPowerGrid/grid-image-model-reference/main/stable_diffusion.json"
DIFFUSERS_URL_DEFAULT = "https://raw.githubusercontent.com/AIPowerGrid/grid-image-model-reference/main/diffusers.json"

ACCEPTED_BASELINES = {
	"stable diffusion 1",
	"stable diffusion 2",
	"stable diffusion 2 512",
	"stable_diffusion_xl",
	"stable_cascade",
	"flux_1",
}


def download_reference() -> dict:
	ref_url = os.getenv("HORDE_IMAGE_COMPVIS_REFERENCE", REF_URL_DEFAULT)
	diff_url = os.getenv("HORDE_IMAGE_DIFFUSERS_REFERENCE", DIFFUSERS_URL_DEFAULT)
	print(f"Reference URL: {ref_url}")
	print(f"Diffusers URL: {diff_url}")
	ref, diff = {}, {}
	resp = requests.get(ref_url, timeout=10)
	resp.raise_for_status()
	ref = resp.json()
	try:
		resp2 = requests.get(diff_url, timeout=10)
		resp2.raise_for_status()
		diff = resp2.json()
	except Exception as e:
		print(f"Note: could not load diffusers reference ({e}) - continuing with compvis only")
	ref.update(diff)
	return ref


def build_stable_names(reference: dict) -> Set[str]:
	stable = set()
	for name, info in reference.items():
		baseline = info.get("baseline")
		if baseline in ACCEPTED_BASELINES:
			stable.add(name)
	return stable


def simulate_parse_models(worker_models: List[str], stable_names: Set[str], user_special=False, user_customizer=False, testing_models: Set[str]=None) -> Set[str]:
	if testing_models is None:
		testing_models = set()
	accepted = set()
	for model in worker_models:
		parts = model.split("::")
		if user_special and len(parts) == 2:
			accepted.add(model)
		elif (model in stable_names) or user_customizer or (model in testing_models):
			accepted.add(model)
		else:
			print(f"Rejecting unknown model: {model}")
	return accepted


def run_scenario(worker_models: List[str], simulate_ref_failure: bool=False):
	print("=== Scenario ===")
	print(f"Worker advertises: {worker_models}")
	reference = {}
	stable_names = set()
	try:
		if not simulate_ref_failure:
			reference = download_reference()
			stable_names = build_stable_names(reference)
			print(f"Reference models: {len(reference)} | stable_names: {len(stable_names)}")
		else:
			print("Simulating server reference failure (empty stable_names)")
			reference = {}
			stable_names = set()
	except Exception as e:
		print(f"Reference download/parse failed: {e}")
		stable_names = set()  # replicate failure path on server

	accepted = simulate_parse_models(worker_models, stable_names)
	if not accepted:
		print("RESULT: 400 BadRequest -> 'Unfortunately we cannot accept workers serving unrecognised models at this time'")
	else:
		print(f"RESULT: OK. Accepted models: {sorted(list(accepted))}")


def main():
	# Use the exact models from your failing worker payload
	default_worker_models = [
		"FLUX.1-dev-Kontext-fp8-scaled",
		"flux.1-krea-dev",
		"Chroma",
	]
	if len(sys.argv) > 1:
		try:
			default_worker_models = json.loads(sys.argv[1])
		except Exception as e:
			print(f"Could not parse argv[1] as JSON list, using defaults. Error: {e}")

	print("\n=== Test: Normal (should succeed if reference loads and names match) ===")
	run_scenario(default_worker_models, simulate_ref_failure=False)

	print("\n=== Test: Simulated server failure (reproduces 400) ===")
	run_scenario(default_worker_models, simulate_ref_failure=True)

if __name__ == "__main__":
	main()
