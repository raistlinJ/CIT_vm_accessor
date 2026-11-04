"""Material categories and external material links from Proxmox VM notes."""
import html
from urllib.parse import urlsplit

from transfer_policy import parse_accessforge_metadata


def parse_project_metadata(notes):
    metadata = parse_accessforge_metadata(notes)
    if metadata is None and isinstance(notes, str):
        metadata = parse_accessforge_metadata(html.unescape(notes))
    metadata = metadata or {}
    category = metadata.get("Material Category")
    category = category.strip() if isinstance(category, str) else ""
    category = category or "Misc"
    materials = []
    entries = metadata.get("materials")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
                continue
            url = entry["url"].strip()
            # These URLs are rendered as links; the server never fetches them.
            if not url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in url):
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in ("https", "http") or not parsed.hostname:
                    continue
                parsed.port  # Reject malformed/out-of-range ports as well.
            except ValueError:
                continue
            title = entry.get("title")
            title = title.strip() if isinstance(title, str) else ""
            materials.append({"title": title or url, "url": url})
    return {"category": category, "materials": materials}


def collect_project_metadata(vms, scenario):
    """Group visible VM links by category, deduplicating URLs within each category."""
    project = {"name": scenario, "categories": []}
    categories = {}
    seen_urls = {}
    for vm in sorted(vms, key=lambda vm: int(vm["vmid"])):
        metadata = parse_project_metadata(vm.get("notes"))
        category = metadata["category"]
        for material in metadata["materials"]:
            if category not in categories:
                group = {"name": category, "materials": []}
                categories[category] = group
                seen_urls[category] = set()
                project["categories"].append(group)
            if material["url"] not in seen_urls[category]:
                seen_urls[category].add(material["url"])
                categories[category]["materials"].append(material)
    return project
