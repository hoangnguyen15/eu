"""Data access lives in app/services/* using SQLAlchemy 2 async sessions directly;
queries are small and transactional boundaries matter more than an extra
repository abstraction. Kept as a package for future extraction."""
