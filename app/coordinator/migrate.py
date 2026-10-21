"""Print additive schema by default. --apply is an explicit operator-only operation."""
import argparse
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import postgresql
from .. import db
from .models import Base


def sql():
    dialect = postgresql.dialect()
    statements = []
    for table in Base.metadata.sorted_tables:
        statements.append(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip() + ";")
        for index in sorted(table.indexes, key=lambda i: i.name):
            statements.append(str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)).strip() + ";")
    return "-- Additive only; review on a Neon development branch before production.\n" + "\n\n".join(statements) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Explicitly create only pilot tables on configured database")
    args = parser.parse_args(argv)
    if args.apply:
        Base.metadata.create_all(db.engine)
    else:
        print(sql())


if __name__ == "__main__":
    main()
