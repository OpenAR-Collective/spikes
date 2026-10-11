"""The python-direct side of the engine bake-off: the Python command path in
plain SQL through psycopg, with no Django in the bench or engine processes.

Foundation-written for round 2 (see the round 1 report's limits: nobody had
tried to speed up Python's write path). It shows how much of the Python
side's cost comes from Django's object layer. It keeps Andy Troiano's event
store and reader logic, his hashing, his allocation and waterfall functions,
and his command models, and re-expresses only the database access. Django
still runs the migrations, so the schema is the Python side's exactly. The
twin tests in tests/test_direct_twin.py hold this path to the Django path's
events and state.
"""
