import sys
from pathlib import Path as _P
_PKG = _P(__file__).resolve().parents[1]
sys.path[:0] = [str(_PKG), str(_PKG.parent)]  # pipeline scripts + repo root

import csv
import tempfile
import unittest
from pathlib import Path

from backlinks import config, queries


class QueryTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        config.EDGES_CSV = tmp / "edges.csv"
        config.QUERY_DB = tmp / "q.sqlite3"
        with open(config.EDGES_CSV, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "generation", "source_id", "source_domain", "target_id",
                "target_domain", "source_cc_harmonic_rank",
                "source_cc_harmonic_score", "source_cc_pagerank_rank",
                "source_cc_pagerank_score", "target_cc_harmonic_rank",
                "target_cc_harmonic_score", "target_cc_pagerank_rank",
                "target_cc_pagerank_score",
            ])
            w.writerow([1, 1, "com.a.www", 10, "com.site", 5, 10.0, 3, 0.3, 1, 20.0, 2, 0.8])
            w.writerow([1, 2, "com.b", 11, "com.site.www", 4, 11.0, 4, 0.4, 2, 19.0, 3, 0.7])
            w.writerow([1, 2, "com.b", 12, "com.site.shop", 4, 11.0, 4, 0.4, 3, 18.0, 4, 0.6])
            w.writerow([1, 3, "com.c", 13, "com.sitex", 3, 12.0, 5, 0.5, 4, 17.0, 5, 0.5])
            w.writerow([2, 4, "com.d", 1, "com.a.www", 2, 13.0, 6, 0.6, 5, 16.0, 6, 0.4])
            w.writerow([1, 1, "com.a.www", 14, "com.rival", 5, 10.0, 3, 0.3, 6, 9.0, 7, 0.2])
            w.writerow([1, 3, "com.c", 14, "com.rival", 3, 12.0, 5, 0.5, 6, 9.0, 7, 0.2])
            w.writerow([1, 3, "com.c", 13, "com.sitex", 3, 12.0, 5, 0.5, 4, 17.0, 5, 0.5])
            w.writerow([1, 5, "com.e", 14, "com.rival", 1, 30.0, 1, 0.5, 6, 9.0, 7, 0.2])
            w.writerow([1, 5, "com.e", 13, "com.sitex", 1, 30.0, 1, 0.5, 4, 17.0, 5, 0.5])
        queries.build_index()

    def test_counts_include_subdomains_not_lookalikes(self):
        res = queries.backlink_counts(["www.site.com"])[0]
        self.assertEqual(res["backlink_count"], 3)
        self.assertEqual(res["unique_referring_hosts"], 2)

    def test_list_and_common_crawl_rank(self):
        self.assertEqual(len(queries.list_backlinks("site.com", 1, 10)), 3)
        self.assertEqual(
            queries.common_crawl_host_rank("site.com")["cc_pagerank_score"],
            0.8,
        )
        self.assertEqual(
            queries.top_common_crawl_hosts("cc_pagerank_score", 1)[0]["host"],
            "site.com",
        )

    def test_backlink_gap(self):
        res = queries.backlink_gap("site.com", ["rival.com", "sitex.com"])
        hosts = [g["host"] for g in res["gap"]]
        self.assertEqual(hosts, ["e.com", "c.com"])  # a.www and b already link to site
        self.assertEqual(res["gap"][0]["competitors_linked"], 2)
        self.assertEqual(res["gap"][0]["cc_harmonic_score"], 30.0)
        self.assertEqual(res["site_referring_hosts"], 2)
        with self.assertRaises(ValueError):
            queries.backlink_gap("site.com", [])

    def test_invalid_domain(self):
        with self.assertRaises(ValueError):
            queries.normalize_domain("bad domain")


class VisualGraphTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        config.VISUAL_NODES_CSV = tmp / "nodes.csv"
        config.VISUAL_EDGES_CSV = tmp / "edges.csv"
        config.VISUAL_GEXF = tmp / "graph.gexf"
        config.FOLLOWUP_SUMMARY_CSV = tmp / "summary.csv"
        with open(config.VISUAL_NODES_CSV, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["domain", "hostname", "level", "role", "in_degree", "out_degree",
                        "cc_harmonic_rank", "cc_harmonic_score",
                        "cc_pagerank_rank", "cc_pagerank_score"])
            w.writerow(["com.site.www", "www.site.com", 0, "client", 2, 0, "", "", "", ""])
            w.writerow(["com.other", "other.com", 0, "client", 1, 0, "", "", "", ""])
            w.writerow(["com.a", "a.com", 1, "gen1_source", 1, 1, 5, "", 7, ""])
            w.writerow(["com.b", "b.com", 1, "gen1_nonfollow_source", 0, 1, 9, "", 9, ""])
            w.writerow(["com.c", "c.com", 2, "gen2_source", 0, 1, 3, "", 3, ""])
            w.writerow(["com.z", "z.com", 1, "gen1_source", 0, 1, 1, "", 1, ""])
        with open(config.VISUAL_EDGES_CSV, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["source_domain", "target_domain", "weight", "generation", "link_type"])
            w.writerow(["com.a", "com.site.www", 1, 1, "follow"])
            w.writerow(["com.b", "com.site.www", 2, 1, "nofollow"])
            w.writerow(["com.c", "com.a", 1, 2, "follow"])
            w.writerow(["com.z", "com.other", 1, 1, "follow"])
        with open(config.FOLLOWUP_SUMMARY_CSV, "w", newline="") as f:
            f.write("classification,count\nfollow,10\nnofollow,3\n")

    def test_full_graph(self):
        g = queries.visual_graph()
        self.assertEqual(g["total_edges"], 4)
        self.assertEqual(g["edges_by_link_type"], {"follow": 3, "nofollow": 1})
        self.assertEqual(g["nodes_by_level"], {0: 2, 1: 3, 2: 1})

    def test_client_subtree_and_filters(self):
        g = queries.visual_graph(client="site.com")
        self.assertEqual({e["source"] for e in g["edges"]}, {"a.com", "b.com", "c.com"})
        self.assertEqual(queries.visual_graph("site.com", max_level=1)["total_edges"], 2)
        only_follow = queries.visual_graph("site.com", link_types=["follow"])
        self.assertEqual(only_follow["total_edges"], 2)
        self.assertTrue(queries.visual_graph(limit=1)["truncated"])
        with self.assertRaises(ValueError):
            queries.visual_graph(client="nope.com")
        with self.assertRaises(ValueError):
            queries.visual_graph(link_types=["dofollow"])

    def test_followup_summary(self):
        self.assertEqual(queries.followup_summary()[0], {"classification": "follow", "count": 10})


if __name__ == "__main__":
    unittest.main()
