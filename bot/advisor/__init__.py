"""The DeepSeek advisor: a second opinion on ready rows that may only remove or shrink automatic entries.

The deterministic engine decides. The advisor never creates a row, never raises a size, never
turns a refusal into an entry and never touches the order gate, the mandate, the halt or the
stops. Its calls run on one thread of their own, outside the cycle's lock; the cycle only reads
a copy of the verdict cache and queues requests without waiting.
"""
