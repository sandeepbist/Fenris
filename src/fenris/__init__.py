"""Fenris: RAG over your own Google Drive.

Drive is the authoritative source; the local SQLite store is a rebuildable
read projection. Every chunk is keyed by revision, not by file.
"""

__version__ = "0.1.0"
