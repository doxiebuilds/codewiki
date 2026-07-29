from codewiki.indexer.domain.base import index_domain, register, registered, unregister
from codewiki.indexer.domain import builtin  # noqa: F401 — registers the built-in extractors

__all__ = ["index_domain", "register", "registered", "unregister"]
