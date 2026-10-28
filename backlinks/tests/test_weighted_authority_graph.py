import sys
from pathlib import Path as _P
_PKG = _P(__file__).resolve().parents[1]
sys.path[:0] = [str(_PKG), str(_PKG.parent)]  # pipeline scripts + repo root

import csv
import tempfile
import unittest
from pathlib import Path

from weighted_authority_graph import (
    STATUS_WEIGHTS,
    build_weighted_authority_edges,
    classification_weight,
)


class WeightedAuthorityGraphTests(unittest.TestCase):
    def write_csv(self, path, fields, rows):
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_requested_classifications_and_fallbacks(self):
        for classification, expected in STATUS_WEIGHTS.items():
            with self.subTest(classification=classification):
                self.assertEqual(classification_weight(classification), expected)

        self.assertEqual(classification_weight("http_418"), 0.0)
        self.assertEqual(classification_weight(""), 0.0)

    def test_combines_all_generation_one_and_later_dofollow_edges(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            all_edges = root / "all.csv"
            statuses = root / "status.csv"
            dofollow = root / "dofollow.csv"
            output = root / "weighted.csv"

            self.write_csv(
                all_edges,
                [
                    "source_domain", "target_domain", "weight", "generation",
                    "source_cc_pagerank_score", "target_cc_pagerank_score",
                ],
                [
                    {"source_domain": "gen1-follow", "target_domain": "target", "weight": "2", "generation": "1", "source_cc_pagerank_score": "0.8", "target_cc_pagerank_score": "0.9"},
                    {"source_domain": "gen1-403", "target_domain": "target", "weight": "4", "generation": "1", "source_cc_pagerank_score": "0.7", "target_cc_pagerank_score": "0.9"},
                    {"source_domain": "gen1-nofollow", "target_domain": "target", "weight": "3", "generation": "1", "source_cc_pagerank_score": "0.6", "target_cc_pagerank_score": "0.9"},
                    {"source_domain": "gen2-not-follow", "target_domain": "target", "weight": "5", "generation": "2", "source_cc_pagerank_score": "0.5", "target_cc_pagerank_score": "0.9"},
                ],
            )
            self.write_csv(
                statuses,
                ["source_domain", "target_domain", "classification"],
                [
                    {"source_domain": "gen1-follow", "target_domain": "target", "classification": "nofollow"},
                    {"source_domain": "gen1-follow", "target_domain": "target", "classification": "follow"},
                    {"source_domain": "gen1-403", "target_domain": "target", "classification": "http_403"},
                    {"source_domain": "gen1-nofollow", "target_domain": "target", "classification": "sponsored"},
                ],
            )
            self.write_csv(
                dofollow,
                [
                    "source_domain", "target_domain", "weight", "generation",
                    "source_cc_pagerank_score", "target_cc_pagerank_score",
                ],
                [
                    {"source_domain": "gen1-dofollow-export", "target_domain": "target", "weight": "7", "generation": "1", "source_cc_pagerank_score": "0.4", "target_cc_pagerank_score": "0.9"},
                    {"source_domain": "gen2-dofollow", "target_domain": "target", "weight": "6", "generation": "2", "source_cc_pagerank_score": "0.3", "target_cc_pagerank_score": "0.9"},
                    {"source_domain": "gen3-dofollow", "target_domain": "target", "weight": "8", "generation": "3", "source_cc_pagerank_score": "0.2", "target_cc_pagerank_score": "0.9"},
                ],
            )

            counts = build_weighted_authority_edges(
                all_edges, statuses, dofollow, output
            )
            with output.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(counts, (3, 2))
        self.assertEqual(len(rows), 5)
        by_source = {row["source_domain"]: row for row in rows}
        self.assertEqual(by_source["gen1-follow"]["status_classification"], "follow")
        self.assertEqual(float(by_source["gen1-follow"]["weight"]), 2.0)
        self.assertEqual(by_source["gen1-follow"]["source_cc_pagerank_score"], "0.8")
        self.assertEqual(float(by_source["gen1-403"]["weight"]), 3.0)
        self.assertEqual(float(by_source["gen1-nofollow"]["weight"]), 0.0)
        self.assertNotIn("gen2-not-follow", by_source)
        self.assertNotIn("gen1-dofollow-export", by_source)
        self.assertEqual(by_source["gen2-dofollow"]["generation"], "2")
        self.assertEqual(by_source["gen2-dofollow"]["source_cc_pagerank_score"], "0.3")
        self.assertEqual(by_source["gen3-dofollow"]["status_classification"], "follow")

    def test_missing_generation_one_status_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            all_edges = root / "all.csv"
            statuses = root / "status.csv"
            dofollow = root / "dofollow.csv"

            self.write_csv(
                all_edges,
                ["source_domain", "target_domain", "weight", "generation"],
                [{"source_domain": "source", "target_domain": "target", "weight": "1", "generation": "1"}],
            )
            self.write_csv(
                statuses,
                ["source_domain", "target_domain", "classification"],
                [],
            )
            self.write_csv(
                dofollow,
                ["source_domain", "target_domain", "weight", "generation"],
                [],
            )

            with self.assertRaisesRegex(RuntimeError, "No follow-status rows"):
                build_weighted_authority_edges(
                    all_edges, statuses, dofollow, root / "weighted.csv"
                )


if __name__ == "__main__":
    unittest.main()
