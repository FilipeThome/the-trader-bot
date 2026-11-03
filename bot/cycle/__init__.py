"""The parts of one cycle the ``Runtime`` delegates to: market reads, exits, the risk level,
scoring and the DCA and grid robots.

No module here imports ``bot.runtime``: the ``Runtime`` owns the lock, the halt, the ledger
and the config, and passes them in on every call (its own bound methods, looked up at call
time, so a test that patches one on the instance or the class is still obeyed). A module
writes the ledger only through the ``ledger`` it is given.
"""
