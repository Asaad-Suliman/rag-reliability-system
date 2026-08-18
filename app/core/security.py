"""Authentication and authorization primitives.

Empty by design: JWT issuing/verification, password hashing and the current-user
dependency arrive in Step 04 (API and Auth). The module exists so the package
layout is stable and imports do not move later.
"""
