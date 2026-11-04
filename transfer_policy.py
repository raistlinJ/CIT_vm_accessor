"""Strict, namespaced file-transfer policy stored in Proxmox VM notes."""
import json


def _unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate metadata key")
        obj[key] = value
    return obj


def parse_accessforge_metadata(description):
    """Read exactly one unambiguous AccessForge object from VM notes."""
    if not isinstance(description, str):
        return None
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
                return None
            cursor = start + 1
            continue
        if isinstance(obj, dict) and "AccessForge" in obj:
            policies.append(obj["AccessForge"])
        cursor = start + length
    if len(policies) != 1 or not isinstance(policies[0], dict):
        return None
    return policies[0]


def parse_transfer_policy(description):
    disabled = {"file_upload": False, "file_download": False}
    metadata = parse_accessforge_metadata(description)
    if metadata is None:
        return disabled
    return {key: metadata.get(key) is True for key in disabled}
