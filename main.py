import arxiv
import time
import logging
import datetime
from pathlib import Path
import json
import yaml
import collections
import regex
import random  # Needed for retry backoff

# -------------------------
# Setup logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# -------------------------
# Configuration
# -------------------------
CONFIG_PATH = "config.yaml"
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
OUTPUT_JSON = OUTPUT_DIR / "papers_with_specificities.json"
CATEGORY_COUNTS_JSON = OUTPUT_DIR / "category_counts.json"


# -------------------------
# Helper functions
# -------------------------
def load_config(config_path=CONFIG_PATH):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_keywords_from_yaml(file_path="keywords.yaml"):
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def exact_match(text, keyword_list):
    """Exact keyword match with optional pluralization."""
    textlower = text.lower()
    matches = []
    for keywords in keyword_list:
        split_list = keywords.split(',')
        for keyword in split_list:
            pattern = r'\b' + regex.escape(keyword.strip()) + r's?\b'
            if regex.search(pattern, textlower, regex.IGNORECASE):
                matches.append(split_list[0])
                break
    return matches


def extract_specificities(text, architecture_keywords, application_keywords):
    architectures = exact_match(text, architecture_keywords)
    applications = exact_match(text, application_keywords)
    return architectures, applications


def get_paper_key(paper):
    """Unique key for deduplication."""
    title = paper["title"].strip().lower()
    first_author = paper["authors"][0].strip().lower() if paper["authors"] else ""
    return (title, first_author)


def get_start_date(json_path: Path, default_start_date: str = "2024-01-01") -> datetime.date:
    """Return the date to start scraping from."""
    if not json_path.exists():
        return datetime.date.fromisoformat(default_start_date)

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            papers = json.load(f)
    except json.JSONDecodeError:
        return datetime.date.fromisoformat(default_start_date)

    if not papers:
        return datetime.date.fromisoformat(default_start_date)

    try:
        latest_date = max(datetime.datetime.fromisoformat(p["published"]).date() for p in papers)
        return latest_date + datetime.timedelta(days=1)
    except Exception:
        return datetime.date.fromisoformat(default_start_date)


def search_arxiv(keywords_list, start_date, max_results=100, max_retries=3):
    """Query arXiv API."""
    client = arxiv.Client()
    all_results = []

    for keywords in keywords_list:
        query = ' AND '.join(f'"{kw}"' for kw in keywords)
        search = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending
        )

        logger.info(f"Running query: {query}")
        attempt = 0
        while attempt < max_retries:
            try:
                for result in client.results(search):
                    if result.published.date() >= start_date:
                        paper = {
                            "title": result.title,
                            "url": result.entry_id,
                            "authors": [author.name for author in result.authors],
                            "published": result.published.strftime("%Y-%m-%d"),
                            "abstract": result.summary,
                            "categories": [],
                            "architectures": [],
                            "applications": []
                        }
                        all_results.append(paper)
                break
            except Exception as e:
                attempt += 1
                wait_time = 2 ** attempt + random.uniform(0, 1)
                logger.warning(f"Error on attempt {attempt} for query '{query}': {e}")
                if attempt < max_retries:
                    logger.info(f"Retrying in {wait_time:.2f} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Failed after {max_retries} attempts: {query}")
    return all_results


def count_categories(papers):
    counter = collections.Counter()
    for paper in papers:
        for category in paper["categories"]:
            counter[category] += 1
    return counter


# -------------------------
# Main
# -------------------------
def main():
    config = load_config()
    keywords = load_keywords_from_yaml()
    max_results = config.get("max_results", 100)
    categories = config["categories"]

    # Load existing papers
    if OUTPUT_JSON.exists():
        try:
            with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
                existing_papers = json.load(f)
            logger.info(f"Loaded {len(existing_papers)} existing papers.")
        except json.JSONDecodeError:
            logger.warning("Existing JSON corrupted, starting fresh.")
            existing_papers = []
    else:
        existing_papers = []

    # Compute start date for scraping
    start_date = get_start_date(OUTPUT_JSON, default_start_date=config.get("start_date", "2024-01-01"))
    logger.info(f"Scraping from date: {start_date.isoformat()}")

    # Add unique key and prepare 'is_recent' flag
    seven_days_ago = datetime.date.today() - datetime.timedelta(days=7)

    seen = {}
    for p in existing_papers:
        # Parse published date
        pub_date = datetime.date.fromisoformat(p["published"])
        # Mark as recent if within the last 7 days
        p["is_recent"] = pub_date >= seven_days_ago
        # Add to seen dict
        seen[get_paper_key(p)] = p

    # Sort existing papers by published date descending
    existing_papers_sorted = sorted(seen.values(), key=lambda x: x["published"], reverse=True)
    existing_papers = existing_papers_sorted

    """
    # Load existing papers
    if OUTPUT_JSON.exists():
        try:
            with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
                existing_papers = json.load(f)
            logger.info(f"Loaded {len(existing_papers)} existing papers.")
        except json.JSONDecodeError:
            logger.warning("Existing JSON corrupted, starting fresh.")
            existing_papers = []
    else:
        existing_papers = []

    start_date = get_start_date(OUTPUT_JSON, default_start_date=config.get("start_date", "2024-01-01"))
    logger.info(f"Scraping from date: {start_date.isoformat()}")

    seen = {get_paper_key(p): p for p in existing_papers}
    """

    # Scrape each category
    for category_name, cat_info in categories.items():
        logger.info(f"Searching category: {category_name}")
        cat_papers = search_arxiv(cat_info["queries"], start_date=start_date, max_results=max_results)
        for paper in cat_papers:
            paper["categories"].append(category_name)
            key = get_paper_key(paper)
            if key not in seen:
                seen[key] = paper
            else:
                existing = seen[key]
                existing["categories"] = list(set(existing["categories"]) | set(paper["categories"]))

    # Add architectures and applications
    unique_papers = list(seen.values())
    for paper in unique_papers:
        archs, apps = extract_specificities(
            paper["abstract"],
            keywords["architectures"],
            keywords["applications"]
        )
        paper["architectures"] = archs
        paper["applications"] = apps

    # Save output
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(unique_papers, f, indent=2)
    logger.info(f"Saved {len(unique_papers)} papers to {OUTPUT_JSON}")

    category_counts = count_categories(unique_papers)
    with open(CATEGORY_COUNTS_JSON, "w", encoding="utf-8") as f:
        json.dump(category_counts, f, indent=2)
    logger.info("Category counts updated.")


if __name__ == "__main__":
    main()
