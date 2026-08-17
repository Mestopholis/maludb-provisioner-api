"""The migration tool a customer runs (ADR-042).

Deliberately separate from `services.control_plane`: this package runs on the
customer's machine, against their Supabase project, and holds no platform
credential. It reaches the destination only through the public API any client
could call, which is what keeps ADR-039's ceiling meaningful.
"""
