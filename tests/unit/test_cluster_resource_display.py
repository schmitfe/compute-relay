"""Saved partition display only: no scheduler, live filesystem or job access."""

from html.parser import HTMLParser
from types import SimpleNamespace

from django.template.loader import render_to_string
import pytest

from compute_relay.control import scheduler_display
from compute_relay.control.scheduler_display import partition_resources


def node(name, partition="", **values):
    return {"name": name, "partition": partition, "cpus": "32", "memory_mib": "256000", "gres": "gpu:a100:4", **values}


def groups(snapshot):
    return {group["name"]: group for group in partition_resources(snapshot)}


def render_resources(snapshot):
    cluster = SimpleNamespace(id="synthetic-cluster", name="Synthetic cluster", owner_id=2, worker_id="worker",
        worker=SimpleNamespace(name="Worker"), discovery_state="ready", configuration_confirmed=True, setup_complete=True,
        backend="slurm", discovery_snapshot=snapshot, configuration={"ssh": {"user": "fixture", "host": "fixture.invalid"}})
    rendered = render_to_string("broker/cluster_detail.html", {"cluster": cluster, "scheduler_partitions": partition_resources(snapshot),
        "snapshot_text": "saved JSON", "software": {}, "profile": {}, "csrf_token": "synthetic-token",
        "user": SimpleNamespace(id=1, is_authenticated=False)})
    return rendered.split("<summary>Saved scheduler resources</summary>", 1)[1].split("</section>", 1)[0]


def test_nodes_are_grouped_per_partition_and_overlaps_are_counted_independently():
    snapshot = {"nodes": [node("n01", "cpu*"), node("n02", "cpu,gpu"), node("n02", "gpu"), node("n03", "gpu")]}
    partitions = groups(snapshot)
    assert partitions["cpu"]["node_count"] == partitions["gpu"]["node_count"] == 2
    assert [entry["name"] for entry in partitions["cpu"]["nodes"]] == ["n01", "n02"]
    assert [entry["name"] for entry in partitions["gpu"]["nodes"]] == ["n02", "n03"]


def test_compressed_partition_nodelists_assign_saved_nodes_without_partition_column():
    snapshot = {"partitions": [{"PartitionName": "cpu", "Nodes": "node[001-003,005]"},
                                {"PartitionName": "gpu", "Nodes": "node[003-004]"}],
                "nodes": [node(f"node{number:03}") for number in range(1, 6)]}
    partitions = groups(snapshot)
    assert partitions["cpu"]["node_count"] == 4 and partitions["gpu"]["node_count"] == 2
    assert [entry["name"] for entry in partitions["cpu"]["nodes"]] == ["node001", "node002", "node003", "node005"]
    assert [entry["name"] for entry in partitions["gpu"]["nodes"]] == ["node003", "node004"]
    assert "Unspecified partition" not in partitions


def test_compressed_node_rows_count_hosts_and_deduplicate_overlapping_ranges():
    partitions = groups({"nodes": [node("r[1-2]n[01-03]", "cpu"), node("r1n02,r2n03", "cpu")]})
    assert partitions["cpu"]["node_count"] == 6
    assert partitions["cpu"]["nodes"][0]["name"] == "r[1-2]n[01-03]"


def test_partial_compressed_row_membership_does_not_show_other_partitions_nodes():
    partitions = groups({"partitions": [{"PartitionName": "gpu", "Nodes": "n[01-02]"}],
                         "nodes": [node("n[01-04]")]})
    assert partitions["gpu"]["node_count"] == 2
    assert partitions["gpu"]["nodes"][0]["name"] == "n01, n02"


@pytest.mark.parametrize("expression", ["node[1-1000000000]", "node[3-1]", "node[01-3]", "node[1-4:2]", "node[1-3", "ALL"])
def test_unsupported_or_unbounded_hostlists_do_not_become_invented_counts(expression):
    partition = groups({"nodes": [node(expression, "cpu")]})["cpu"]
    assert partition["node_count"] is None and partition["nodes"][0]["name"] == expression


def test_missing_node_data_is_unknown_but_reported_zero_and_explicit_totals_remain_known():
    snapshot = {"partitions": [{"PartitionName": "unknown"}, {"PartitionName": "empty", "TotalNodes": "0"},
                                {"PartitionName": "reported", "TotalNodes": "16"}]}
    partitions = groups(snapshot)
    assert partitions["unknown"]["node_count"] is None
    assert partitions["empty"]["node_count"] == 0 and partitions["reported"]["node_count"] == 16
    html = render_resources(snapshot)
    assert "Unknown node count" in html and "0 nodes" in html and "16 nodes" in html
    assert "No node inventory reported" in html


def test_conflicting_totals_or_membership_show_unknown_instead_of_a_false_count():
    snapshot = {"partitions": [{"PartitionName": "cpu", "Nodes": "n[1-2]", "TotalNodes": "3"}], "nodes": [node("n1", "cpu")]}
    partition = groups(snapshot)["cpu"]
    assert partition["node_count"] is None and partition["inconsistent"]
    assert "Saved partition totals and node membership differ" in render_resources(snapshot)


def test_output_preserves_resource_values_and_partition_limits_but_hides_cached_states():
    snapshot = {"partitions": [{"PartitionName": "gpu", "TotalNodes": "1", "TotalCPUs": "32", "MaxTime": "2-00:00:00", "State": "DOWN"}],
                "nodes": [node("gpu01", "gpu", state="drain")]}
    html = render_resources(snapshot)
    for expected in ("gpu01", "32", "256000", "gpu:a100:4", "Maximum job time", "2-00:00:00", "1 node"):
        assert expected in html
    assert "DOWN" not in html and "drain" not in html


class ResourceMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.partition_depth, self.details, self.outside_tables = 0, [], 0

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "details" and attributes.get("class") == "partition-resources":
            self.partition_depth += 1
            self.details.append(attributes)
        if tag == "table" and not self.partition_depth:
            self.outside_tables += 1

    def handle_endtag(self, tag):
        if tag == "details" and self.partition_depth:
            self.partition_depth -= 1


def test_every_partition_is_collapsed_with_its_count_and_all_node_tables_are_inside():
    snapshot = {"nodes": [node("c01", "cpu"), node("g01", "gpu"), node("g02", "gpu")]}
    html = render_resources(snapshot)
    parsed = ResourceMarkup()
    parsed.feed(html)
    assert [details["data-partition"] for details in parsed.details] == ["cpu", "gpu"]
    assert all("open" not in details for details in parsed.details) and parsed.outside_tables == 0
    assert html.count("<table>") == 2 and "1 node" in html and "2 nodes" in html


def test_partition_and_node_strings_are_escaped_in_resource_markup():
    html = render_resources({"nodes": [node("<script>alert(1)</script>", '<unsafe "partition">')]})
    assert "<script>" not in html and "<unsafe" not in html
    assert "&lt;script&gt;" in html and "&lt;unsafe &quot;partition&quot;&gt;" in html


def test_display_work_is_bounded_without_fabricating_truncated_node_counts(monkeypatch):
    monkeypatch.setattr(scheduler_display, "MAX_NODE_ROWS", 1)
    partition = groups({"nodes": [node("n1", "cpu"), node("n2", "cpu")]})["cpu"]
    assert partition["node_count"] is None and partition["details_limited"] and len(partition["nodes"]) == 1
    monkeypatch.setattr(scheduler_display, "MAX_NODE_ROWS", 20000)
    monkeypatch.setattr(scheduler_display, "MAX_NODE_VISITS", 2)
    partition = groups({"nodes": [node("n[1-3]", "cpu")]})
    assert partition == {}


@pytest.mark.parametrize("snapshot", [None, [], "invalid", {}, {"partitions": None, "nodes": None}])
def test_absent_or_legacy_invalid_snapshot_is_safe(snapshot):
    assert partition_resources(snapshot) == []
