"""Context graph: documents, sections and entities linked by references, so retrieval can follow multi-hop links.

  adapters.py   turn source documents into a Graph (MarkdownReferenceAdapter: deterministic)
  store.py      persist / load a tenant's graph in Postgres
  retriever.py  hybrid search for seed sections, then expand along graph edges
"""
