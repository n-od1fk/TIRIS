from logging.config import fileConfig

from alembic import context

from app import models  # noqa: F401 - registers table metadata
from app.database import Base, engine

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

if context.is_offline_mode():
    context.configure(url=engine.url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:

    def migrate(connection):
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            compare_type=True,
            version_table_schema=config.attributes.get("test_schema"),
        )
        with context.begin_transaction():
            context.run_migrations()

    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        migrate(provided_connection)
    else:
        with engine.connect() as connection:
            migrate(connection)
