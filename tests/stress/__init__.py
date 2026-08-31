"""B4-prep peak-throttle load + latency suite.

Every module here is ``pytest.mark.stress`` and excluded from the default run
(`-m 'not stress'`); execute with ``uv run pytest -m stress``. These prove the
B1-B3 foundation (bounded queue, single-writer graph lock, pure-CPU L3, tight
event loop) holds under load an order of magnitude past normal before B4 puts
in-process LLM inference on the same loop.
"""
