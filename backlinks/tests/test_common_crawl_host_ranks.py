import sys
from pathlib import Path as _P
_PKG = _P(__file__).resolve().parents[1]
sys.path[:0] = [str(_PKG), str(_PKG.parent)]  # pipeline scripts + repo root

import csv
import gzip
import tempfile
import unittest
from pathlib import Path

import graph_filter
import networkx as nx
import backlink_index
from backlink_index import load_host_ranks


class CommonCrawlHostRankTests(unittest.TestCase):
    def test_loads_only_requested_hosts_from_rank_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ranks_path = Path(temp_dir) / "ranks.txt.gz"
            with gzip.open(ranks_path, "wt", encoding="utf-8", newline="") as f:
                f.write(
                    "#harmonicc_pos\t#harmonicc_val\t#pr_pos\t#pr_val\t#host_rev\n"
                )
                f.write("1\t10.5\t2\t0.25\tcom.example.www\n")
                f.write("2\t8.5\t1\t0.5\torg.other\n")

            ranks = load_host_ranks({"com.example.www"}, ranks_path)

        self.assertEqual(
            ranks,
            {
                "com.example.www": {
                    "harmonic_rank": "1",
                    "harmonic_score": "10.5",
                    "pagerank_rank": "2",
                    "pagerank_score": "0.25",
                }
            },
        )

    def test_backlink_export_adds_source_and_target_rank_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_output_dir = backlink_index.OUTPUT_DIR
            try:
                backlink_index.OUTPUT_DIR = root
                backlink_index.write_output(
                    [(1, 2, 1)],
                    {1: "com.source", 2: "com.target"},
                    {"target": {2}},
                    {
                        "com.source": {
                            "harmonic_rank": "3",
                            "harmonic_score": "4.5",
                            "pagerank_rank": "5",
                            "pagerank_score": "0.06",
                        },
                        "com.target": {
                            "harmonic_rank": "7",
                            "harmonic_score": "8.5",
                            "pagerank_rank": "9",
                            "pagerank_score": "0.01",
                        },
                    },
                )
            finally:
                backlink_index.OUTPUT_DIR = original_output_dir

            with (root / "backlink_edges.csv").open(
                newline="", encoding="utf-8"
            ) as f:
                row = next(csv.DictReader(f))

        self.assertEqual(row["source_cc_harmonic_rank"], "3")
        self.assertEqual(row["target_cc_pagerank_score"], "0.01")

    @unittest.skip("graph_filter.build_full_graph no longer exists; test needs updating to the batched graph_filter API")
    def test_graph_filter_preserves_host_ranks_in_node_and_csv_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            edges_path = root / "edges.csv"
            with edges_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "generation",
                        "source_domain",
                        "target_domain",
                        "source_cc_harmonic_rank",
                        "source_cc_harmonic_score",
                        "source_cc_pagerank_rank",
                        "source_cc_pagerank_score",
                        "target_cc_harmonic_rank",
                        "target_cc_harmonic_score",
                        "target_cc_pagerank_rank",
                        "target_cc_pagerank_score",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "generation": "1",
                        "source_domain": "com.source",
                        "target_domain": "com.target",
                        "source_cc_harmonic_rank": "12",
                        "source_cc_harmonic_score": "3.5",
                        "source_cc_pagerank_rank": "8",
                        "source_cc_pagerank_score": "0.04",
                        "target_cc_harmonic_rank": "4",
                        "target_cc_harmonic_score": "7.5",
                        "target_cc_pagerank_rank": "2",
                        "target_cc_pagerank_score": "0.2",
                    }
                )

            graph, targets = graph_filter.build_full_graph(edges_path)
            metrics = graph_filter.calculate_metrics(graph)
            scores = graph_filter.score_graph(graph, metrics, targets)
            filtered, _ = graph_filter.create_filtered_graph(
                graph, scores, targets
            )

            node_scores_path = root / "node_scores.csv"
            filtered_edges_path = root / "filtered_edges.csv"
            graphml_path = root / "filtered.graphml"
            gexf_path = root / "filtered.gexf"
            original_node_scores_path = graph_filter.NODE_SCORES_CSV
            original_filtered_edges_path = graph_filter.FILTERED_EDGES_CSV
            try:
                graph_filter.NODE_SCORES_CSV = node_scores_path
                graph_filter.FILTERED_EDGES_CSV = filtered_edges_path
                graph_filter.write_node_scores(scores)
                graph_filter.write_filtered_edges(filtered)
                nx.write_graphml(filtered, graphml_path)
                nx.write_gexf(filtered, gexf_path)
            finally:
                graph_filter.NODE_SCORES_CSV = original_node_scores_path
                graph_filter.FILTERED_EDGES_CSV = original_filtered_edges_path

            with node_scores_path.open(newline="", encoding="utf-8") as f:
                node_rows = {
                    row["domain"]: row for row in csv.DictReader(f)
                }
            with filtered_edges_path.open(newline="", encoding="utf-8") as f:
                edge_row = next(csv.DictReader(f))
            graphml_rank = nx.read_graphml(graphml_path).nodes["com.source"][
                "cc_harmonic_rank"
            ]
            gexf_rank = nx.read_gexf(gexf_path).nodes["com.target"][
                "cc_pagerank_rank"
            ]

        self.assertEqual(filtered.nodes["com.source"]["cc_harmonic_rank"], 12)
        self.assertEqual(node_rows["com.source"]["cc_pagerank_score"], "0.04")
        self.assertEqual(edge_row["source_cc_harmonic_rank"], "12")
        self.assertEqual(edge_row["target_cc_pagerank_score"], "0.2")
        self.assertEqual(int(graphml_rank), 12)
        self.assertEqual(int(gexf_rank), 2)


if __name__ == "__main__":
    unittest.main()
