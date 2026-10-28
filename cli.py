#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

import seo_crawler as crawler


def normalize_url(url):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def main():
    parser = argparse.ArgumentParser(
        description="SEO crawler with automatic location-weighted TF-IDF keyword discovery"
    )

    parser.add_argument(
        "primary",
        help="Primary website or sitemap URL"
    )
    parser.add_argument(
        "competitors",
        nargs="*",
        help="Competitor website or sitemap URLs"
    )
    parser.add_argument(
        "--output", "-o",
        default=str(crawler.OUTPUT_DIR),
        help=f"Output directory (default: {crawler.OUTPUT_DIR})"
    )
    parser.add_argument(
        "--threads", "-t",
        type=int,
        default=8,
        help="Crawler worker threads (default: 8)"
    )
    parser.add_argument(
        "--delay", "-d",
        type=float,
        default=0.25,
        help="Minimum request delay per domain in seconds (default: 0.25)"
    )
    parser.add_argument(
        "--max-ngram",
        type=int,
        choices=(1, 2, 3),
        default=3,
        help="Maximum keyword phrase size: 1, 2, or 3 words (default: 3)"
    )
    parser.add_argument(
        "--min-df",
        type=int,
        default=2,
        help="Minimum number of pages a term must appear on (default: 2)"
    )
    parser.add_argument(
        "--top-terms",
        type=int,
        default=150,
        help="Maximum rows in the root keyword reports (default: 150)"
    )

    args = parser.parse_args()

    if not 1 <= args.threads <= 32:
        parser.error("--threads must be between 1 and 32")
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    if args.min_df < 1:
        parser.error("--min-df must be at least 1")
    if args.top_terms < 1:
        parser.error("--top-terms must be at least 1")

    primary = normalize_url(args.primary)
    competitors = [normalize_url(url) for url in args.competitors]

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    crawler.OUTPUT_DIR = output_dir
    crawler.MAX_WORKERS = args.threads
    crawler.REQUEST_DELAY_SECONDS = args.delay

    print("\n" + "=" * 64)
    print("SEO Crawler - Automatic Weighted TF-IDF")
    print("=" * 64)
    print(f"Primary:          {primary}")
    print(f"Competitors:      {len(competitors)}")
    print(f"Output:           {output_dir}")
    print(f"Threads:          {args.threads}")
    print(f"Request delay:    {args.delay:.2f}s")
    print(f"Maximum n-gram:   {args.max_ngram}")
    print(f"Minimum DF:       {args.min_df}")
    print(f"Report limit:     {args.top_terms}")
    print("\nLocation weights:")
    for location, weight in crawler.LOCATION_WEIGHTS.items():
        print(f"  {location:16} {weight:g}x")
    print("=" * 64 + "\n")

    try:
        crawler.run_multi_site_pipeline(
            primary,
            competitors,
            max_ngram=args.max_ngram,
            min_df=args.min_df,
            report_limit=args.top_terms,
        )
    except KeyboardInterrupt:
        print("\nCrawler interrupted by user.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nError running crawler: {exc}")
        sys.exit(1)

    print("\nCompleted SEO crawl + automatic keyword analysis.")
    print(f"Results saved to: {output_dir}")


if __name__ == "__main__":
    main()
