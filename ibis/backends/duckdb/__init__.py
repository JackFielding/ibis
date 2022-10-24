"""DuckDB backend."""

from __future__ import annotations

import ast
import itertools
import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping, MutableMapping

import pandas as pd
import pyarrow as pa
import pyarrow.types as pat
import sqlalchemy as sa
import toolz

import ibis.expr.datatypes as dt
from ibis.backends.base.sql.alchemy.datatypes import to_sqla_type

if TYPE_CHECKING:
    import duckdb

import ibis.expr.schema as sch
import ibis.expr.types as ir
from ibis.backends.base.sql.alchemy import BaseAlchemyBackend
from ibis.backends.duckdb.compiler import DuckDBSQLCompiler
from ibis.backends.duckdb.datatypes import parse
from ibis.common.dispatch import RegexDispatcher

_generate_view_code = RegexDispatcher("_register")
_dialect = sa.dialects.postgresql.dialect()

_gen_table_names = (f"registered_table{i:d}" for i in itertools.count())


def _name_from_path(path: Path) -> str:
    base, *_ = path.name.partition(os.extsep)
    return base.replace("-", "_")


def _name_from_dataset(dataset: pa.dataset.FileSystemDataset) -> str:
    return _name_from_path(Path(os.path.commonprefix(dataset.files)))


def _quote(name: str):
    return _dialect.identifier_preparer.quote(name)


@_generate_view_code.register(r"parquet://(?P<path>.+)", priority=10)
def _parquet(_, path, table_name=None):
    path = Path(path).absolute()
    table_name = table_name or _name_from_path(path)
    quoted_table_name = _quote(table_name)
    return (
        f"CREATE OR REPLACE VIEW {quoted_table_name} as SELECT * from read_parquet('{path}')",  # noqa: E501
        table_name,
    )


@_generate_view_code.register(r"csv(?:\.gz)?://(?P<path>.+)", priority=10)
def _csv(_, path, table_name=None):
    path = Path(path).absolute()
    table_name = table_name or _name_from_path(path)
    quoted_table_name = _quote(table_name)
    return (
        f"CREATE OR REPLACE VIEW {quoted_table_name} as SELECT * from read_csv_auto('{path}')",  # noqa: E501
        table_name,
    )


@_generate_view_code.register(r"(?:file://)?(?P<path>.+)", priority=9)
def _file(_, path, table_name=None):
    num_sep_chars = len(os.extsep)
    extension = "".join(Path(path).suffixes)[num_sep_chars:]
    return _generate_view_code(f"{extension}://{path}", table_name=table_name)


@_generate_view_code.register(r"s3://.+", priority=10)
def _s3(full_path, table_name=None):
    # TODO: gate this import once the ResultHandler mixin is merged #4454
    import pyarrow.dataset as ds

    dataset = ds.dataset(full_path)
    table_name = table_name or _name_from_dataset(dataset)
    quoted_table_name = _quote(table_name)
    return quoted_table_name, dataset


@_generate_view_code.register(r".+", priority=1)
def _default(_, **kwargs):
    raise ValueError(
        """
Unrecognized filetype or extension.
Valid prefixes are parquet://, csv://, or file://

Supported filetypes are parquet, csv, and csv.gz
    """
    )


class Backend(BaseAlchemyBackend):
    name = "duckdb"
    compiler = DuckDBSQLCompiler

    def current_database(self) -> str:
        return "main"

    @staticmethod
    def _convert_kwargs(kwargs: MutableMapping) -> None:
        read_only = kwargs.pop("read_only", "False").capitalize()
        try:
            kwargs["read_only"] = ast.literal_eval(read_only)
        except ValueError as e:
            raise ValueError(
                f"invalid value passed to ast.literal_eval: {read_only!r}"
            ) from e

    @property
    def version(self) -> str:
        # TODO: there is a `PRAGMA version` we could use instead
        import importlib.metadata

        return importlib.metadata.version("duckdb")

    def do_connect(
        self,
        database: str | Path = ":memory:",
        path: str | Path = None,
        read_only: bool = False,
        **config: Any,
    ) -> None:
        """Create an Ibis client connected to a DuckDB database.

        Parameters
        ----------
        database
            Path to a duckdb database.
        read_only
            Whether the database is read-only.
        config
            DuckDB configuration parameters. See the [DuckDB configuration
            documentation](https://duckdb.org/docs/sql/configuration) for
            possible configuration values.

        Examples
        --------
        >>> import ibis
        >>> ibis.duckdb.connect("database.ddb", threads=4, memory_limit="1GB")
        """
        if path is not None:
            warnings.warn(
                "The `path` argument is deprecated in 4.0. Use `database=...` "
                "instead."
            )
            database = path
        if not (in_memory := database == ":memory:"):
            database = Path(database).absolute()
        super().do_connect(
            sa.create_engine(
                f"duckdb:///{database}",
                connect_args=dict(read_only=read_only, config=config),
                poolclass=sa.pool.SingletonThreadPool if in_memory else None,
            )
        )
        self._meta = sa.MetaData(bind=self.con)

    def register(
        self,
        source: str | Path | Any,
        table_name: str | None = None,
    ) -> ir.Table:
        """Register a data source as a table in the current database.

        Parameters
        ----------
        source
            The data source. May be a path to a file or directory of
            parquet/csv files, a pandas dataframe, or a pyarrow table or
            dataset.
        table_name
            An optional name to use for the created table. This defaults to the
            filename if a path (with hyphens replaced with underscores), or
            sequentially generated name otherwise.

        Returns
        -------
        ir.Table
            The just-registered table
        """
        if isinstance(source, str) and source.startswith("s3://"):
            table_name, dataset = _generate_view_code(source, table_name=table_name)
            # We don't create a view since DuckDB special cases Arrow Datasets
            # so if we also create a view we end up with both a "lazy table"
            # and a view with the same name
            with self._safe_raw_sql("SELECT 1") as cursor:
                # DuckDB normally auto-detects Arrow Datasets that are defined
                # in local variables but the `dataset` variable won't be local
                # by the time we execute against this so we register it
                # explicitly.
                cursor.cursor.c.register(table_name, dataset)
        elif isinstance(source, (str, Path)):
            sql, table_name = _generate_view_code(source, table_name=table_name)
            self.con.execute(sql)
        else:
            if table_name is None:
                table_name = next(_gen_table_names)
            self.con.execute("register", (table_name, source))

        return self.table(table_name)

    def to_pyarrow_batches(
        self,
        expr: ir.Expr,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        limit: int | str | None = None,
        chunk_size: int = 1_000_000,
    ) -> Iterator[pa.RecordBatch]:
        _ = self._import_pyarrow()
        query_ast = self.compiler.to_ast_ensure_limit(expr, limit, params=params)
        sql = query_ast.compile()

        cursor = self.raw_sql(sql)

        _reader = cursor.cursor.fetch_record_batch(chunk_size=chunk_size)
        # Horrible hack to make sure cursor isn't garbage collected
        # before batches are streamed out of the RecordBatchReader
        batches = IbisRecordBatchReader(_reader, cursor)
        return batches
        # TODO: duckdb seems to not care about the `chunk_size` argument
        # and returns batches in 1024 row chunks

    def to_pyarrow(
        self,
        expr: ir.Expr,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        limit: int | str | None = None,
    ) -> pa.Table:
        _ = self._import_pyarrow()
        query_ast = self.compiler.to_ast_ensure_limit(expr, limit, params=params)
        sql = query_ast.compile()

        cursor = self.raw_sql(sql)
        table = cursor.cursor.fetch_arrow_table()

        if isinstance(expr, ir.Table):
            return table
        elif isinstance(expr, ir.Column):
            # Column will be a ChunkedArray, `combine_chunks` will
            # flatten it
            if len(table.columns[0]):
                return table.columns[0].combine_chunks()
            else:
                return pa.array(table.columns[0])
        elif isinstance(expr, ir.Scalar):
            return table.columns[0][0]
        else:
            raise ValueError

    def fetch_from_cursor(
        self,
        cursor: duckdb.DuckDBPyConnection,
        schema: sch.Schema,
    ):
        table = cursor.cursor.fetch_arrow_table()
        df = pd.DataFrame(
            {
                name: (
                    col.to_pylist()
                    if pat.is_nested(col.type)
                    else col.to_pandas(timestamp_as_object=True)
                )
                for name, col in zip(table.column_names, table.columns)
            }
        )
        return schema.apply_to(df)

    def _metadata(self, query: str) -> Iterator[tuple[str, dt.DataType]]:
        for name, type, null in toolz.pluck(
            ["column_name", "column_type", "null"],
            self.con.execute(f"DESCRIBE {query}"),
        ):
            ibis_type = parse(type)
            yield name, ibis_type(nullable=null.lower() == "yes")

    def _get_schema_using_query(self, query: str) -> sch.Schema:
        """Return an ibis Schema from a DuckDB SQL string."""
        return sch.Schema.from_tuples(self._metadata(query))

    def _register_in_memory_table(self, table_op):
        df = table_op.data.to_frame()
        self.con.execute("register", (table_op.name, df))

    def _get_sqla_table(
        self,
        name: str,
        schema: str | None = None,
        **kwargs: Any,
    ) -> sa.Table:
        with warnings.catch_warnings():
            # don't fail or warn if duckdb-engine fails to discover types
            warnings.filterwarnings(
                "ignore",
                message="Did not recognize type",
                category=sa.exc.SAWarning,
            )
            # We don't rely on index reflection, ignore this warning
            warnings.filterwarnings(
                "ignore",
                message="duckdb-engine doesn't yet support reflection on indices",
            )

            table = super()._get_sqla_table(name, schema, **kwargs)

        nulltype_cols = frozenset(
            col.name for col in table.c if isinstance(col.type, sa.types.NullType)
        )

        if not nulltype_cols:
            return table

        quoted_name = self.con.dialect.identifier_preparer.quote(name)

        for colname, type in self._metadata(quoted_name):
            if colname in nulltype_cols:
                # replace null types discovered by sqlalchemy with non null
                # types
                table.append_column(
                    sa.Column(
                        colname,
                        to_sqla_type(type),
                        nullable=type.nullable,
                    ),
                    replace_existing=True,
                )
        return table

    def _get_temp_view_definition(
        self,
        name: str,
        definition: sa.sql.compiler.Compiled,
    ) -> str:
        return f"CREATE OR REPLACE TEMPORARY VIEW {name} AS {definition}"


class IbisRecordBatchReader(pa.RecordBatchReader):
    def __init__(self, reader, cursor):
        self.reader = reader
        self.cursor = cursor

    def close(self):
        self.reader.close()
        del self.cursor

    def read_all(self):
        return self.reader.read_all()

    def read_next_batch(self):
        return self.reader.read_next_batch()

    def read_pandas(self):
        return self.reader.read_pandas()

    def schema(self):
        return self.reader.schema