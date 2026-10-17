"""ReAct agent: an LLM that answers by looping think -> pick a tool -> observe, up to a step limit.

  tools.py  hybrid_search, follow_links, lookup_entity, read_section + the numbered source registry
  react.py  the loop, guardrails and the grounded final answer
"""
