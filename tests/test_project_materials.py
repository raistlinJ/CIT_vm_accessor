import html
import json
from unittest.mock import patch

import pytest

import main
from project_metadata import collect_project_metadata, parse_project_metadata
from test_spice import client, response
from transfer_policy import parse_transfer_policy


def notes(category="Networking project", materials=None, scenario="network-lab"):
    return json.dumps({
        "Scenario": scenario, "User": "student", "Pass": "guest-password",
        "AccessForge": {
            "file_upload": True, "file_download": False, "Material Category": category,
            "materials": materials if materials is not None else [
                {"title": "Lab instructions", "url": "https://example.edu/lab"},
                {"title": "Reference PDF", "url": "https://example.edu/reference.pdf"},
            ],
        },
    })


@pytest.mark.parametrize("wrap", [lambda text: text, html.escape,
                                 lambda text: "<p>" + html.escape(text) + "</p>"])
def test_project_metadata_supports_notes_formats_and_preserves_existing_fields(wrap):
    source = notes()
    metadata = parse_project_metadata(wrap(source))
    assert metadata["category"] == "Networking project"
    assert [item["title"] for item in metadata["materials"]] == ["Lab instructions", "Reference PDF"]
    assert main._extract_vm_credentials(wrap(source)) == {"username": "student", "password": "guest-password"}
    assert main._extract_scenario(wrap(source)) == "network-lab"
    assert parse_transfer_policy(source) == {"file_upload": True, "file_download": False}


@pytest.mark.parametrize("source", [None, "", "Human notes", "{broken}",
    '{"AccessForge":true}', '{"AccessForge":{"materials":"https://example.edu"}}',
    '{"AccessForge":{"Material Category":123,"materials":{}}}',
    '{"AccessForge":{"Material Category":"one","Material Category":"two"}}',
    '{"AccessForge":{"Material Category":"one"}}\n{"AccessForge":{"Material Category":"two"}}',
    '{broken "AccessForge":{"Material Category":"nested"}}',
])
def test_missing_invalid_or_ambiguous_metadata_is_ignored(source):
    assert parse_project_metadata(source) == {"category": "Misc", "materials": []}


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,hello", "file:///tmp/file",
    "//example.edu/file", "/file.pdf", "https://", "https://[bad", "https://example.edu:bad",
    "https://example.edu:99999", "https://example.edu/with space", "https://example.edu/a\nb",
    "https://example.edu/a\x00b", "https://example.edu/a\\b"])
def test_materials_reject_unsafe_or_invalid_urls(url):
    assert parse_project_metadata(notes(materials=[{"title": "bad", "url": url}]))["materials"] == []


def test_bad_materials_do_not_hide_valid_entries_and_missing_titles_use_url():
    metadata = parse_project_metadata(notes(category="  A project  ", materials=[
        None, "a string", {}, {"url": 123},
        {"url": " https://example.edu/file.pdf "},
        {"title": " Guide ", "url": "http://example.edu/?a=1&b=2"},
    ]))
    assert metadata == {"category": "A project", "materials": [
        {"title": "https://example.edu/file.pdf", "url": "https://example.edu/file.pdf"},
        {"title": "Guide", "url": "http://example.edu/?a=1&b=2"},
    ]}


def test_project_aggregation_is_stable_and_deduplicates_by_url():
    project = collect_project_metadata([
        {"vmid": 102, "notes": notes(category="Other name", materials=[
            {"title": "Duplicate", "url": "https://example.edu/lab"},
            {"title": "Another file", "url": "https://example.edu/other.pdf"},
        ])},
        {"vmid": "101", "notes": notes()},
    ], "network-lab")
    assert project["name"] == "network-lab"
    assert [group["name"] for group in project["categories"]] == ["Networking project", "Other name"]
    assert [item["title"] for item in project["categories"][0]["materials"]] == [
        "Lab instructions", "Reference PDF"]
    assert [item["title"] for item in project["categories"][1]["materials"]] == ["Duplicate", "Another file"]
    assert collect_project_metadata([{"vmid": 101, "notes": "Scenario: legacy"}], "legacy") == {
        "name": "legacy", "categories": []}


def test_links_combine_within_category_and_remain_available_in_other_categories():
    project = collect_project_metadata([
        {"vmid": 103, "notes": notes(category="", materials=[{"title": "Misc copy", "url": "https://example.edu/lab"}])},
        {"vmid": 102, "notes": notes(category=" Networking ", materials=[
            {"title": "Duplicate", "url": "https://example.edu/lab"},
            {"title": "Second guide", "url": "https://example.edu/second"},
        ])},
        {"vmid": 101, "notes": notes(category="Networking", materials=[{"title": "First guide", "url": "https://example.edu/lab"}])},
    ], "Scenario heading")
    assert project == {"name": "Scenario heading", "categories": [
        {"name": "Networking", "materials": [
            {"title": "First guide", "url": "https://example.edu/lab"},
            {"title": "Second guide", "url": "https://example.edu/second"},
        ]},
        {"name": "Misc", "materials": [{"title": "Misc copy", "url": "https://example.edu/lab"}]},
    ]}


@pytest.mark.parametrize("value", ["", "   ", None, 123])
def test_blank_or_invalid_category_uses_misc(value):
    source = json.dumps({"AccessForge": {"Material Category": value}})
    assert parse_project_metadata(source)["category"] == "Misc"


def render_home(client, vm_notes, visible_ids):
    vms = [{"vmid": vmid, "type": "qemu", "node": "pve", "name": f"VM {vmid}",
            "status": "running"} for vmid in vm_notes]

    def get(path, **kwargs):
        if path == "/cluster/resources":
            return response(vms)
        if path == "/access/permissions":
            return response({f"/vms/{vmid}": {"VM.Console": 1} for vmid in visible_ids})
        raise AssertionError(path)

    def fetch(vm, *args):
        return vm["vmid"], vm_notes[vm["vmid"]]

    with patch.object(main, "proxmox_get", side_effect=get), \
            patch.object(main, "fetch_vm_notes", side_effect=fetch):
        return client.get("/")


def test_home_renders_materials_in_correct_project_and_preserves_scenario_actions(client):
    result = render_home(client, {
        101: notes(),
        102: notes(category="", materials=[]),
        103: notes(category="", materials=[{"title": "Other guide", "url": "https://example.edu/other"}], scenario="other-lab"),
        104: notes(category="Hidden name", materials=[{"title": "Secret guide", "url": "https://example.edu/secret"}]),
    }, [101, 102, 103])
    assert result.status_code == 200
    body = result.get_data(as_text=True)
    groups = body.split('<div class="scenario-section">')[1:]
    assert len(groups) == 2
    network, other = groups
    assert '<span class="project-name">network-lab</span>' in network
    assert '<summary>Networking project</summary>' in network
    assert '<details class="material-category">' in network
    assert 'aria-label="Materials for network-lab"' in network
    assert network.index("Lab instructions") < network.index('<div class="vm-list">')
    assert 'href="https://example.edu/reference.pdf" target="_blank" rel="noopener noreferrer"' in network
    assert 'data-scenario="network-lab"' in network
    assert network.count("Lab instructions") == 1
    assert "VM 101" in network and "VM 102" in network
    assert "Other guide" not in network
    assert '<span class="project-name">other-lab</span>' in other
    assert "Other guide" in other and "Lab instructions" not in other
    assert "<summary>Misc</summary>" in other
    assert "Hidden name" not in body and "Secret guide" not in body and "VM 104" not in body
    with client.session_transaction() as session:
        assert session["backend_map"]["network-lab"] == ["104"]


def test_home_escapes_names_and_titles_and_preserves_link_query(client):
    result = render_home(client, {101: notes(category='<img src=x onerror="alert(1)">', materials=[
        {"title": "<script>alert(1)</script>", "url": 'https://example.edu/?a=1&b="two"'},
        {"title": "Unsafe", "url": "javascript:alert(1)"},
    ])}, [101])
    body = result.get_data(as_text=True)
    assert '<img src=x onerror="alert(1)">' not in body
    assert "&lt;img src=x onerror=" in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert 'href="https://example.edu/?a=1&amp;b=&#34;two&#34;"' in body
    assert "javascript:alert(1)" not in body


def test_home_renders_a_dropdown_for_each_material_category(client):
    result = render_home(client, {
        101: notes(category="Guides", materials=[{"title": "Guide", "url": "https://example.edu/guide"}]),
        102: notes(category="Downloads", materials=[{"title": "Download", "url": "https://example.edu/download"}]),
        103: notes(category="  ", materials=[{"title": "Uncategorized", "url": "https://example.edu/misc"}]),
    }, [101, 102, 103])
    body = result.get_data(as_text=True)
    groups = [part.split('</details>', 1)[0] for part in body.split('<details class="material-category">')[1:]]
    assert len(groups) == 3
    for group, category, link in zip(groups, ["Guides", "Downloads", "Misc"], ["guide", "download", "misc"]):
        assert f'<summary>{category}</summary>' in group
        assert f'href="https://example.edu/{link}"' in group
        assert group.count('<li>') == 1
    assert '<span class="project-name">network-lab</span>' in body


def test_legacy_project_does_not_render_empty_materials_section(client):
    result = render_home(client, {101: '{"Scenario":"Legacy lab"}'}, [101])
    body = result.get_data(as_text=True)
    assert '<span class="project-name">Legacy lab</span>' in body
    assert '<section class="project-materials"' not in body


def test_empty_home_still_renders(client):
    assert render_home(client, {}, []).status_code == 200
