"""Job search providers.

One interface so the screens never learn which service is behind them.
"""

from app.engines.jobsearch.apify import ApifyJobSearch
from app.engines.jobsearch.provider import (
    JobSearchProviderProtocol,
    SearchQuery,
    SearchResult,
    SearchRun,
    SearchStatus,
)

__all__ = [
    "ApifyJobSearch",
    "JobSearchProviderProtocol",
    "SearchQuery",
    "SearchResult",
    "SearchRun",
    "SearchStatus",
]
