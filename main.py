import arxiv
import time
import logging
import datetime
from pathlib import Path
import json
import yaml
import collections
import regex
import random
import argparse
from datetime import datetime as dt

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def load_config(config_path="config.yaml"):
    """Load configuration settings from a YAML file

    Args:
        config_path (str): Path to the configuration file

    Returns:
        dict: Parsed configuration dictionary
    """
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def load_keywords_from_yaml(file_path="keywords.yaml"):
    """Load keyword definitions from a YAML file

    Args:
        file_path (str): Path to the YAML file containing keywords

    Returns:
        dict: Dictionary with 'architectures' and 'applications' keyword lists
    """
    with open(file_path, "r") as file:
        return yaml.safe_load(file)

def exact_match(text, keyword_list):
    """Perform exact keyword matching

    Args:
        text (str): Text in which to search for keywords
        keyword_list (list): List of keyword strings

    Returns:
        list: List of first keywords found
    """
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
    """Extract architectures and applications from a given text

    Args:
        text (str): text to analyze
        architecture_keywords (list): List of architecture-related keyword groups
        application_keywords (list): List of application-related keyword groups

    Returns:
        tuple: (architectures, applications) lists matched in the text
    """
    architectures = exact_match(text, architecture_keywords)
    applications = exact_match(text, application_keywords)
    return architectures, applications

def get_paper_key(paper):
    """Generate a unique key for a paper based on title and first author

    Args:
        paper (dict): Paper dictionary with 'title' and 'authors'

    Returns:
        tuple: Key used for deduplication (title, first author)
    """
    title = paper["title"].strip().lower()
    first_author = paper["authors"][0].strip().lower() if paper["authors"] else ""
    return (title, first_author)

def search_arxiv(keywords_list, start_date, max_results=100, max_retries=3):
    """Query arXiv API for papers matching keyword queries and date criteria

    Args:
        keywords_list (list): List of keyword lists to use for querying
        start_date (datetime.date): Minimum date of paper publication
        max_results (int): Maximum number of results to retrieve per query
        max_retries (int): Retry count in case of API failure

    Returns:
        list: List of retrieved paper dictionaries
    """
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
    """Count the number of papers per category

    Args:
        papers (list): List of paper dictionaries

    Returns:
        Counter: Mapping from category names to counts
    """
    counter = collections.Counter()
    for paper in papers:
        for category in paper["categories"]:
            counter[category] += 1
    return counter

def main():
    """Main function to orchestrate the scraping, filtering, and saving process."""

    config = load_config()
    keywords = load_keywords_from_yaml()
    max_results = config["max_results"]
    categories = config["categories"]

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "papers_with_specificities.json"

    if output_path.exists():
        with open(output_path, "r", encoding="utf-8") as f:
            existing_papers = json.load(f)
        logger.info(f"Loaded {len(existing_papers)} existing papers.")
    else:
        existing_papers = []

    nb_existing_papers = len(existing_papers)

    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", type=str, help="Start date in YYYY-MM-DD format or relative like -7d")
    args = parser.parse_args()

    if args.start_date:
        if args.start_date.startswith("-") and args.start_date.endswith("d"):
            days = int(args.start_date[1:-1])
            start_date = datetime.date.today() - datetime.timedelta(days=days)
        else:
            start_date = datetime.date.fromisoformat(args.start_date)
    elif existing_papers:
        latest_date = max(datetime.date.fromisoformat(p["published"]) for p in existing_papers)
        start_date = latest_date - datetime.timedelta(days=1)  # Buffer to avoid missing papers
    else:
        start_date = datetime.date.fromisoformat(config["start_date"])

    logger.info(f"Scraping from date: {start_date.isoformat()}")
    seen = {get_paper_key(p): p for p in existing_papers}

    for category_name, cat_info in categories.items():
        logger.info(f"Searching category: {category_name}")

        cat_papers = search_arxiv(
            cat_info["queries"],
            start_date=start_date,
            max_results=max_results
        )

        for paper in cat_papers:
            paper["categories"].append(category_name)
            key = get_paper_key(paper)

            if key not in seen:
                seen[key] = paper
            else:
                existing = seen[key]
                existing["categories"] = list(set(existing["categories"]) | set(paper["categories"]))

    unique_papers = list(seen.values())
    logger.info(f"Total unique papers after deduplication: {len(unique_papers)}")

    for paper in unique_papers:
        architectures, applications = extract_specificities(
            paper["abstract"],
            keywords["architectures"],
            keywords["applications"]
        )
        paper["architectures"] = architectures
        paper["applications"] = applications

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(unique_papers, f, indent=2)

    logger.info(f"Saved all papers to {output_path}")

    # Save the category counts and date of scrape
    nb_new_papers = len(unique_papers) - nb_existing_papers
    category_counts = count_categories(unique_papers)
    category_summary = {
        "scrape_date": dt.now().strftime("%Y-%m-%d"),
        "new_papers": nb_new_papers,
        "category_counts": category_counts
        }
    with open(output_dir / "category_counts.json", "w", encoding="utf-8") as f:
        json.dump(category_summary, f, indent=2)

    logger.info("Done.")

if __name__ == "__main__":
    main()
