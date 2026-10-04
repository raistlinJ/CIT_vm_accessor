"""Strict, namespaced file-transfer policy stored in Proxmox VM notes."""
import json


def _unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate metadata key")
        obj[key] = value
    return obj


def parse_transfer_policy(description):
    disabled = {"file_upload": False, "file_download": False}
    if not isinstance(description, str):
        return disabled
    decoder = json.JSONDecoder(object_pairs_hook=_unique_keys)
    policies = []
    cursor = 0
    while cursor < len(description):
        start = description.find("{", cursor)
        if start < 0:
            break
        try:
            obj, length = decoder.raw_decode(description[start:])
        except ValueError:
            # A broken policy must not expose an inner JSON object as policy.
            if '"AccessForge"' in description[start:]:
                return disabled
            cursor = start + 1
            continue
        if isinstance(obj, dict) and "AccessForge" in obj:
            policies.append(obj["AccessForge"])
        cursor = start + length
    if len(policies) != 1 or not isinstance(policies[0], dict):
        return disabled
    return {key: policies[0].get(key) is True for key in disabled}
