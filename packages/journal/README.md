# journal

Status: active shared package.

Trade journal models, repositories, query helpers.

Core component (see ADR-0007).
Builds the foundation for winrate, expectancy, all per-strategy stats.

It owns SQLAlchemy models, repositories, and Alembic migrations used by analytics and
execution. Database constraints and migration history are authoritative when this
README and an older architecture description disagree.
