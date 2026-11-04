"""Project display names and external material links from Proxmox VM notes."""
import html
from urllib.parse import urlsplit

from transfer_policy import parse_accessforge_metadata


def parse_project_metadata(notes):
    metadata = parse_accessforge_metadata(notes)
    if metadata is None and isinstance(notes, str):
        metadata = parse_accessforge_metadata(html.unescape(notes))
    metadata = metadata or {}
    name = metadata.get("Project Name")
    name = name.strip() if isinstance(name, str) else ""
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
    return {"name": name, "materials": materials}


def collect_project_metadata(vms, scenario):
    """Combine visible VMs in a Scenario, preferring the lowest VM ID."""
    project = {"name": scenario, "materials": []}
    seen_urls = set()
    named = False
    for vm in sorted(vms, key=lambda vm: int(vm["vmid"])):
        metadata = parse_project_metadata(vm.get("notes"))
        if metadata["name"] and not named:
            project["name"] = metadata["name"]
            named = True
        for material in metadata["materials"]:
            if material["url"] not in seen_urls:
                seen_urls.add(material["url"])
                project["materials"].append(material)
    return project
