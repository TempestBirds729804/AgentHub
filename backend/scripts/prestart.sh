#! /usr/bin/env bash

set -e
set -x

# Let the DB start
python app/backend_pre_start.py

# Run migrations
alembic upgrade head

# LangGraph maintains its own tables outside Alembic.
python app/setup_checkpointer.py

# Create the private document bucket.
python app/setup_storage.py

# Create initial data in DB
python app/initial_data.py
