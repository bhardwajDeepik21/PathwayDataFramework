# Copyright © 2023 Pathway

from __future__ import annotations

import csv
import dataclasses
import itertools
from collections import ChainMap
from collections.abc import Callable, Iterable, KeysView, Mapping
from dataclasses import dataclass
from pydoc import locate
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, get_type_hints
from warnings import warn

import numpy as np
import pandas as pd

from pathway.internals import dtype as dt
from pathway.internals import trace
from pathway.internals.column_properties import ColumnProperties
from pathway.internals.runtime_type_check import runtime_type_check

if TYPE_CHECKING:
    from pathway.internals import column as clmn


def _cls_fields(cls):
    return {k: v for k, v in cls.__dict__.items() if not k.startswith("__")}


def schema_from_columns(
    columns: Mapping[str, clmn.Column],
    _name: str | None = None,
) -> type[Schema]:
    if _name is None:
        _name = "schema_from_columns(" + str(list(columns.keys())) + ")"
    __dict = {
        "__metaclass__": SchemaMetaclass,
        "__annotations__": {name: c.dtype for name, c in columns.items()},
    }
    return _schema_builder(_name, __dict)


def _type_converter(series: pd.Series) -> dt.DType:
    if series.apply(lambda x: isinstance(x, (tuple, list))).all():
        return dt.ANY_TUPLE
    if (series.isna() | series.isnull()).all():
        return dt.NONE
    if (series.apply(lambda x: isinstance(x, np.ndarray))).all():
        return dt.ARRAY
    if pd.api.types.is_integer_dtype(series.dtype):
        ret_type: dt.DType = dt.INT
    elif pd.api.types.is_float_dtype(series.dtype):
        ret_type = dt.FLOAT
    elif pd.api.types.is_bool_dtype(series.dtype):
        ret_type = dt.BOOL
    elif pd.api.types.is_string_dtype(series.dtype):
        ret_type = dt.STR
    elif pd.api.types.is_datetime64_ns_dtype(series.dtype):
        if series.dt.tz is None:
            ret_type = dt.DATE_TIME_NAIVE
        else:
            ret_type = dt.DATE_TIME_UTC
    elif pd.api.types.is_timedelta64_dtype(series.dtype):
        ret_type = dt.DURATION
    elif pd.api.types.is_object_dtype(series.dtype):
        ret_type = dt.ANY
    else:
        ret_type = dt.ANY
    if series.isna().any() or series.isnull().any():
        return dt.Optional(ret_type)
    else:
        return ret_type


def schema_from_pandas(
    dframe: pd.DataFrame,
    *,
    id_from: list[str] | None = None,
    name: str | None = None,
) -> type[Schema]:
    if name is None:
        name = "schema_from_pandas(" + str(dframe.columns) + ")"
    if id_from is None:
        id_from = []
    columns: dict[str, ColumnDefinition] = {
        name: column_definition(dtype=_type_converter(dframe[name]))
        for name in dframe.columns
    }
    for name in id_from:
        columns[name] = dataclasses.replace(columns[name], primary_key=True)

    return schema_builder(
        columns=columns, properties=SchemaProperties(append_only=True), name=name
    )


@runtime_type_check
def schema_from_types(
    _name: str | None = None,
    **kwargs,
) -> type[Schema]:
    """Constructs schema from kwargs: field=type.

    Example:

    >>> import pathway as pw
    >>> s = pw.schema_from_types(foo=int, bar=str)
    >>> s
    <pathway.Schema types={'foo': <class 'int'>, 'bar': <class 'str'>}>
    >>> issubclass(s, pw.Schema)
    True
    """
    if _name is None:
        _name = "schema(" + str(kwargs) + ")"
    __dict = {
        "__metaclass__": SchemaMetaclass,
        "__annotations__": kwargs,
    }
    return _schema_builder(_name, __dict)


def schema_add(*schemas: type[Schema]) -> type[Schema]:
    annots_list = [get_type_hints(schema) for schema in schemas]
    annotations = dict(ChainMap(*annots_list))

    assert len(annotations) == sum([len(annots) for annots in annots_list])

    fields_list = [_cls_fields(schema) for schema in schemas]
    fields = dict(ChainMap(*fields_list))

    assert len(fields) == sum([len(f) for f in fields_list])

    return _schema_builder(
        "_".join(schema.__name__ for schema in schemas),
        {
            "__metaclass__": SchemaMetaclass,
            "__annotations__": annotations,
            "__orig__": {f"__arg{i}__": arg for i, arg in enumerate(schemas)},
            **fields,
        },
    )


def _create_column_definitions(schema: SchemaMetaclass) -> dict[str, ColumnSchema]:
    localns = locals()
    #  Update locals to handle recursive Schema definitions
    localns[schema.__name__] = schema
    annotations = get_type_hints(schema, localns=localns)
    fields = _cls_fields(schema)

    columns = {}

    for name, annotation in annotations.items():
        col_dtype = dt.wrap(annotation)
        column = fields.pop(name, column_definition(dtype=col_dtype))

        if not isinstance(column, ColumnDefinition):
            raise ValueError(
                f"`{name}` should be a column definition, found {type(column)}"
            )

        dtype = column.dtype
        if dtype is None:
            dtype = col_dtype

        if col_dtype != dtype:
            raise TypeError(
                f"type annotation of column `{name}` does not match column definition"
            )

        name = column.name or name

        columns[name] = ColumnSchema(
            primary_key=column.primary_key,
            default_value=column.default_value,
            dtype=dt.wrap(dtype),
            name=name,
            append_only=schema.__properties__.append_only,
        )

    if fields:
        names = ", ".join(fields.keys())
        raise ValueError(f"definitions of columns {names} lack type annotation")

    return columns


@dataclass(frozen=True)
class SchemaProperties:
    append_only: bool = False


class SchemaMetaclass(type):
    __columns__: dict[str, ColumnSchema]
    __properties__: SchemaProperties
    __dtypes__: dict[str, dt.DType]
    __types__: dict[str, Any]

    @trace.trace_user_frame
    def __init__(self, *args, append_only=False, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.__properties__ = SchemaProperties(append_only=append_only)
        self.__columns__ = _create_column_definitions(self)
        self.__dtypes__ = {
            name: column.dtype for name, column in self.__columns__.items()
        }
        self.__types__ = {k: v.typehint for k, v in self.__dtypes__.items()}

    def __or__(self, other: type[Schema]) -> type[Schema]:  # type: ignore
        return schema_add(self, other)  # type: ignore

    def properties(self) -> SchemaProperties:
        return self.__properties__

    def columns(self) -> Mapping[str, ColumnSchema]:
        return MappingProxyType(self.__columns__)

    def column_names(self) -> list[str]:
        return list(self.keys())

    def column_properties(self, name: str) -> ColumnProperties:
        column = self.__columns__[name]
        return ColumnProperties(dtype=column.dtype, append_only=column.append_only)

    def primary_key_columns(self) -> list[str] | None:
        # There is a distinction between an empty set of columns denoting
        # the primary key and None. If any (including empty) set of keys if provided,
        # then it will be used to compute the primary key.
        #
        # For the autogeneration one needs to specify None

        pkey_fields = [
            name for name, column in self.__columns__.items() if column.primary_key
        ]
        return pkey_fields if pkey_fields else None

    def default_values(self) -> dict[str, Any]:
        return {
            name: column.default_value
            for name, column in self.__columns__.items()
            if column.has_default_value()
        }

    def keys(self) -> KeysView[str]:
        return self.__columns__.keys()

    def update_types(self, **kwargs) -> type[Schema]:
        columns: dict[str, ColumnDefinition] = {
            col.name: col.to_definition() for col in self.__columns__.values()
        }
        for name, dtype in kwargs.items():
            if name not in columns:
                raise ValueError(
                    "Schema.update_types() argument name has to be an existing column name."
                )
            columns[name] = dataclasses.replace(columns[name], dtype=dt.wrap(dtype))

        return schema_builder(columns=columns, properties=self.__properties__)

    def __getitem__(self, name) -> ColumnSchema:
        return self.__columns__[name]

    def _dtypes(self) -> Mapping[str, dt.DType]:
        return MappingProxyType(self.__dtypes__)

    def typehints(self) -> Mapping[str, Any]:
        return MappingProxyType(self.__types__)

    def __repr__(self):
        return f"<pathway.Schema types={self.__types__}>"

    def __str__(self):
        col_names = [k for k in self.keys()]
        max_lens = [
            max(len(column_name), len(str(self.__dtypes__[column_name])))
            for column_name in col_names
        ]
        res = " | ".join(
            [
                column_name.ljust(max_len)
                for (column_name, max_len) in zip(col_names, max_lens)
            ]
        )
        res = res.rstrip() + "\n"
        res = res + " | ".join(
            [
                (str(self.__dtypes__[column_name])).ljust(max_len)
                for (column_name, max_len) in zip(col_names, max_lens)
            ]
        )
        return res

    def _as_tuple(self):
        return (self.__properties__, tuple(self.__columns__.items()))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SchemaMetaclass):
            return self._as_tuple() == other._as_tuple()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._as_tuple())

    def generate_class(self, class_name: str | None = None) -> str:
        """Generates class with the definition of given schema and returns it as a string.

        Arguments:
            class_name: name of the class with the schema. If not provided, name created
            during schema generation will be used.
        """

        def render_column_definition(name: str, definition: ColumnDefinition):
            properties = [
                f"{field.name}={repr(definition.__getattribute__(field.name))}"
                for field in dataclasses.fields(definition)
                if field.name not in ("name", "dtype")
                and definition.__getattribute__(field.name) != field.default
            ]

            column_definition = f"\t{name}: dt.{definition.dtype} = pw.column_definition({','.join(properties)})"
            return column_definition

        if class_name is None:
            class_name = self.__name__

        if not class_name.isidentifier():
            warn(
                f'Name {class_name} is not a valid name for a class. Using "CustomSchema" instead'
            )
            class_name = "CustomSchema"

        class_definition = f"class {class_name}(pw.Schema):\n"

        class_definition += "\n".join(
            [
                render_column_definition(name, definition.to_definition())
                for name, definition in self.__columns__.items()
            ]
        )

        return class_definition

    def generate_class_to_file(self, path: str, class_name: str | None = None):
        """Generates class with the definition of given schema and saves it to a file.
        Used for persisting definition for schemas, which were automatically generated.

        Note: This function generates also necessary imports, while
            :func:`~pathway.Schema.generate_class` does not.

        Arguments:
            path: path of the file, in which the schema class definition will be saved.
            class_name: name of the class with the schema. If not provided, name created
            during schema generation will be used.
        """
        class_definition = (
            "import pathway as pw\nfrom pathway.internals import dtype as dt\n\n"
            + self.generate_class(class_name)
        )

        with open(path, mode="w") as f:
            f.write(class_definition)

    def assert_equal_to(
        self,
        other: type[Schema],
        *,
        allow_superset: bool = False,
        ignore_primary_keys: bool = True,
    ) -> None:
        self_dict = self.typehints()
        other_dict = other.typehints()

        # Check if self has all columns of other
        if self_dict.keys() < other_dict.keys():
            missing_columns = other_dict.keys() - self_dict.keys()
            raise AssertionError(f"schema does not have columns {missing_columns}")

        # Check if types of columns are the same
        for col in other_dict:
            assert other_dict[col] == self_dict[col], (
                f"type of column {col} does not match - its type is {self_dict[col]} in {self.__name__}",
                f" and {other_dict[col]} in {other.__name__}",
            )

        # When allow_superset=False, check that self does not have extra columns
        if not allow_superset and self_dict.keys() > other_dict.keys():
            extra_columns = self_dict.keys() - other_dict.keys()
            raise AssertionError(
                f"there are extra columns: {extra_columns} which are not present in the provided schema"
            )

        # Check whether primary keys are the same
        if not ignore_primary_keys:
            assert self.primary_key_columns() == other.primary_key_columns(), (
                f"primary keys in the schemas do not match - they are {self.primary_key_columns()} in {self.__name__}",
                f" and {other.primary_key_columns()} in {other.__name__}",
            )


class Schema(metaclass=SchemaMetaclass):
    """Base class to inherit from when creating schemas.
    All schemas should be subclasses of this one.

    Example:

    >>> import pathway as pw
    >>> t1 = pw.debug.parse_to_table('''
    ...    age  owner  pet
    ... 1   10  Alice  dog
    ... 2    9    Bob  dog
    ... 3    8  Alice  cat
    ... 4    7    Bob  dog''')
    >>> t1.schema
    <pathway.Schema types={'age': <class 'int'>, 'owner': <class 'str'>, 'pet': <class 'str'>}>
    >>> issubclass(t1.schema, pw.Schema)
    True
    >>> class NewSchema(pw.Schema):
    ...   foo: int
    >>> SchemaSum = NewSchema | t1.schema
    >>> SchemaSum
    <pathway.Schema types={'age': <class 'int'>, 'owner': <class 'str'>, 'pet': <class 'str'>, 'foo': <class 'int'>}>
    """

    def __init_subclass__(cls, /, append_only: bool = False, **kwargs) -> None:
        super().__init_subclass__(**kwargs)


def _schema_builder(
    _name: str,
    _dict: dict[str, Any],
    *,
    properties: SchemaProperties = SchemaProperties(),
) -> type[Schema]:
    return SchemaMetaclass(_name, (Schema,), _dict, append_only=properties.append_only)


def is_subschema(left: type[Schema], right: type[Schema]):
    if left.keys() != right.keys():
        return False
    for k in left.keys():
        if not dt.dtype_issubclass(left.__dtypes__[k], right.__dtypes__[k]):
            return False
    return True


class _Undefined:
    def __repr__(self):
        return "undefined"


_no_default_value_marker = _Undefined()


@dataclass(frozen=True)
class ColumnSchema:
    primary_key: bool
    default_value: Any | None
    dtype: dt.DType
    name: str
    append_only: bool

    def has_default_value(self) -> bool:
        return self.default_value != _no_default_value_marker

    def to_definition(self) -> ColumnDefinition:
        return ColumnDefinition(
            primary_key=self.primary_key,
            default_value=self.default_value,
            dtype=self.dtype,
            name=self.name,
        )

    @property
    def typehint(self):
        return self.dtype.typehint


@dataclass(frozen=True)
class ColumnDefinition:
    primary_key: bool = False
    default_value: Any | None = _no_default_value_marker
    dtype: dt.DType | None = dt.ANY
    name: str | None = None

    def __post_init__(self):
        assert self.dtype is None or isinstance(self.dtype, dt.DType)


def column_definition(
    *,
    primary_key: bool = False,
    default_value: Any | None = _no_default_value_marker,
    dtype: Any | None = None,
    name: str | None = None,
) -> Any:  # Return any so that mypy does not complain
    """Creates column definition

    Args:
        primary_key: should column be a part of a primary key.
        default_value: default valuee replacing blank entries. The default value of the
            column must be specified explicitly,
            otherwise there will be no default value.
        dtype: data type. When used in schema class,
            will be deduced from the type annotation.
        name: name of a column. When used in schema class,
            will be deduced from the attribute name.

    Returns:
        Column definition.

    Example:

    >>> import pathway as pw
    >>> class NewSchema(pw.Schema):
    ...   key: int = pw.column_definition(primary_key=True)
    ...   timestamp: str = pw.column_definition(name="@timestamp")
    ...   data: str
    >>> NewSchema
    <pathway.Schema types={'key': <class 'int'>, '@timestamp': <class 'str'>, 'data': <class 'str'>}>
    """
    from pathway.internals import dtype as dt

    return ColumnDefinition(
        dtype=dt.wrap(dtype) if dtype is not None else None,
        primary_key=primary_key,
        default_value=default_value,
        name=name,
    )


def schema_builder(
    columns: dict[str, ColumnDefinition],
    *,
    name: str | None = None,
    properties: SchemaProperties = SchemaProperties(),
) -> type[Schema]:
    """Allows to build schema inline, from a dictionary of column definitions.

    Args:
        columns: dictionary of column definitions.
        name: schema name.
        properties: schema properties.

    Returns:
        Schema

    Example:

    >>> import pathway as pw
    >>> pw.schema_builder(columns={
    ...   'key': pw.column_definition(dtype=int, primary_key=True),
    ...   'data': pw.column_definition(dtype=int, default_value=0)
    ... }, name="my_schema")
    <pathway.Schema types={'key': <class 'int'>, 'data': <class 'int'>}>
    """

    if name is None:
        name = "custom_schema(" + str(list(columns.keys())) + ")"

    __annotations = {name: c.dtype or Any for name, c in columns.items()}

    __dict: dict[str, Any] = {
        "__metaclass__": SchemaMetaclass,
        "__annotations__": __annotations,
        **columns,
    }

    return _schema_builder(name, __dict, properties=properties)


def schema_from_dict(
    columns: dict,
    *,
    name: str | None = None,
    properties: dict | SchemaProperties = SchemaProperties(),
) -> type[Schema]:
    """Allows to build schema inline, from a dictionary of column definitions.
    Compared to pw.schema_builder, this one uses simpler structure of the dictionary,
    which allows it to be loaded from JSON file.

    Args:
        columns: dictionary of column definitions. The keys in this dictionary are names
            of the columns, and the values are either:
            - type of the column
            - dictionary with keys: "dtype", "primary_key", "default_value" and values,
            respectively, type of the column, whether it is a primary key, and column's
            default value.
            The type can be given both by python class, or string with class name - that
            is both int and "int" are accepted.
        name: schema name.
        properties: schema properties, given either as instance of SchemaProperties class
            or a dict specyfing arguments of SchemaProperties class.

    Returns:
        Schema

    Example:

    >>> import pathway as pw
    >>> pw.schema_from_dict(columns={
    ...   'key': {"dtype": "int", "primary_key": True},
    ...   'data': {"dtype": "int", "default_value": 0}
    ... }, name="my_schema")
    <pathway.Schema types={'key': <class 'int'>, 'data': <class 'int'>}>
    """

    def get_dtype(dtype) -> dt.DType:
        if isinstance(dtype, str):
            dtype = locate(dtype)
        return dt.wrap(dtype)

    def create_column_definition(entry):
        if not isinstance(entry, dict):
            entry = {"dtype": entry}
        entry["dtype"] = get_dtype(entry.get("dtype", Any))

        return column_definition(**entry)

    column_definitions = {
        column_name: create_column_definition(value)
        for (column_name, value) in columns.items()
    }

    if isinstance(properties, dict):
        properties = SchemaProperties(**properties)

    return schema_builder(column_definitions, name=name, properties=properties)


def _is_parsable_to(s: str, parse_fun: Callable):
    try:
        parse_fun(s)
        return True
    except ValueError:
        return False


def schema_from_csv(
    path: str,
    *,
    name: str | None = None,
    properties: SchemaProperties = SchemaProperties(),
    delimiter: str = ",",
    comment_character: str | None = None,
    escape: str | None = None,
    num_parsed_rows: int | None = None,
):
    """Allows to generate schema based on a CSV file.
    The names of the columns are taken from the header of the CSV file.
    Types of columns are inferred from the values, by checking if they can be parsed.
    Currently supported types are str, int and float.

    Args:
        path: path to the CSV file.
        name: schema name.
        properties: schema properties.
        delimiter: delimiter used in CSV file. Defaults to ",".
        comment_character: character used in CSV file to denote comments.
          Defaults to None
        escape: escape character used in CSV file. Defaults to None.
        num_parsed_rows: number of rows, which will be parsed when inferring types. When
            set to None, all rows will be parsed. When set to 0, types of all columns
            will be set to str. Defaults to None.

    Returns:
        Schema
    """

    def remove_comments_from_file(f: Iterable[str], comment_char: str | None):
        for line in f:
            if line.lstrip()[0] != comment_char:
                yield line

    with open(path) as f:
        csv_reader = csv.DictReader(
            remove_comments_from_file(f, comment_character),
            delimiter=delimiter,
            escapechar=escape,
            quoting=csv.QUOTE_NONE,
        )
        if csv_reader.fieldnames is None:
            raise ValueError("can't generate Schema based on an empty CSV file")
        column_names = csv_reader.fieldnames
        if num_parsed_rows is None:
            csv_data = list(csv_reader)
        else:
            csv_data = list(itertools.islice(csv_reader, num_parsed_rows))

    def choose_type(entries: list[str]):
        if len(entries) == 0:
            return Any
        if all(_is_parsable_to(s, int) for s in entries):
            return int
        if all(_is_parsable_to(s, float) for s in entries):
            return float
        return str

    column_types = {
        column_name: choose_type([row[column_name] for row in csv_data])
        for column_name in column_names
    }

    columns = {
        column_name: column_definition(dtype=column_types[column_name])
        for column_name in column_names
    }

    return schema_builder(columns, name=name, properties=properties)