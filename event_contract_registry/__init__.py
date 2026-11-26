"""Versioned event contract registry.

A small, dependency-free service that stores versioned event contracts,
consumer registrations and message history, then answers the practically useful
question: *can this producer version be consumed by that consumer version, and
if not, which field breaks?*
"""

__version__ = "1.0.0"
